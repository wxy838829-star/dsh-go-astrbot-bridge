"""把聊天消息转给手机上的 dsh-go，再把结果发回聊天。

用法（QQ / 任意平台）：

    /dsh 帮我看看 /root/workspace/notes.md 写了什么
    /dshgo 查一下今天有什么科技新闻
    /dsh 状态 / /dsh 新对话 / /dsh 停止 / /dsh 帮助

不加指令时，聊天模型也能自己调用 ``dsh_go`` 工具把任务转过去。

这个文件只做 AstrBot 适配：解析消息、管理会话映射、发进度消息。
真正跑任务的是 dsh_client.py（ACP 协议，常驻子进程）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.core.star.filter.command import GreedyStr

from .bridge_client import BridgeClient, BridgeError
from .dsh_client import (
    DshAcpClient,
    DshError,
    DshNotRunningError,
    PromptResult,
    ToolTrace,
    build_command,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "dsh_bin": "dsh",
    "profile": "acp",
    "patch_files": ["~/.dsh/profiles/web/cordis.patch.yml"],
    "extra_args": [],
    "work_dir": "~",
    "permission_mode": "danger-full-access",
    "env_api_keys": "",
    "model": "",
    "auto_approve": True,
    "enable_llm_tool": True,
    "admin_only": True,
    "allowed_users": "",
    "allowed_groups": "",
    "session_scope": "chat",
    "show_progress": True,
    "max_reply_chars": 3500,
    "timeout_seconds": 420,
    "idle_shutdown_minutes": 20,
    "keep_context": True,
    "bridge_url": "",
    "bridge_token": "",
}

HELP_TEXT = """dsh-go 桥接

/dsh <指令>   把指令交给手机上的 dsh-go 执行
/dsh 状态     看引擎连没连上
/dsh 新对话   换一个干净的 dsh 上下文
/dsh 停止     打断正在跑的任务
/dsh 帮助     这条说明

也能不说指令，让聊天模型自己调用 dsh_go 工具转过去。"""

_STATUS_FACTS = ("状态", "status")
_NEW_FACTS = ("新对话", "新会话", "新话题", "new", "reset")
_STOP_FACTS = ("停止", "stop", "取消", "cancel", "中断")
_HELP_FACTS = ("帮助", "help", "?", "？")


class DshGoPlugin(star.Star):
    """把聊天指令转给手机上的 dsh-go，并把结果发回聊天。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config:
            try:
                self.cfg.update(dict(config))
            except Exception:
                self.logger.warning("[dsh-go] 插件配置读取失败，使用默认值")

        self._bridge: BridgeClient | None = None
        self._client: DshAcpClient | None = None
        self._client_lock = asyncio.Lock()
        self._sessions: dict[str, str] = {}
        self._busy: set[str] = set()   # 哪个聊天正在跑（用于去重提示）
        self._inflight = 0             # 真正在跑的任务数（用于空闲回收判断）
        self._idle_task: asyncio.Task | None = None
        self._starting: asyncio.Task | None = None
        self._closed = False

    # ------------------------------------------------------------------ 配置

    def _conf(self, key: str, default: Any = None) -> Any:
        value = self.cfg.get(key, DEFAULT_CONFIG.get(key, default))
        return default if value is None else value

    def _patch_list(self) -> list[str]:
        raw = self._conf("patch_files", [])
        if isinstance(raw, str):
            raw = [x for x in raw.replace("\n", ",").split(",") if x.strip()]
        return [os.path.expanduser(str(x)) for x in (raw or []) if str(x).strip()]

    def _extra_args(self) -> list[str]:
        raw = self._conf("extra_args", [])
        if isinstance(raw, str):
            raw = raw.split()
        return [str(x) for x in (raw or []) if str(x).strip()]

    def _work_dir(self) -> str:
        return os.path.expanduser(str(self._conf("work_dir", "~") or "~"))

    def _dsh_bin(self) -> str:
        raw = str(self._conf("dsh_bin", "dsh") or "dsh").strip()
        if os.path.isabs(raw):
            return raw
        found = shutil.which(raw)
        if found:
            return found
        for guess in ("/usr/local/bin/dsh", "/usr/bin/dsh", "/opt/node/bin/dsh"):
            if os.path.exists(guess):
                return guess
        return raw

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        mode = str(self._conf("permission_mode", "danger-full-access") or "").strip()
        if mode:
            env["DSH_PERMISSION_MODE"] = mode
        raw = str(self._conf("env_api_keys", "") or "")
        for line in raw.replace(",", "\n").replace(";", "\n").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and value:
                env[key] = value
        return env

    def _allow(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """检查这个用户/群能不能用 dsh-go。"""
        if not self._conf("enabled", True):
            return False, "dsh-go 桥接已关闭（插件配置 enabled 设为 false）。"

        allow_users = _split_ids(self._conf("allowed_users", ""))
        allow_groups = _split_ids(self._conf("allowed_groups", ""))
        sender = _sender_id(event)
        group = _group_id(event)

        if allow_users:
            if sender in allow_users:
                return True, ""
            return False, "你这个账号不在插件的 allowed_users 白名单里。"
        if allow_groups and group:
            if group in allow_groups:
                return True, ""
            return False, "这个群不在插件的 allowed_groups 白名单里。"
        if self._conf("admin_only", True):
            try:
                if event.is_admin():
                    return True, ""
            except Exception:
                self.logger.debug("[dsh-go] 判断管理员身份失败", exc_info=True)
            return False, "这个指令只允许 AstrBot 管理员使用（可在插件配置里关掉 admin_only）。"
        return True, ""

    def _scope_key(self, event: AstrMessageEvent) -> str:
        """聊天会话的键：默认按会话（群/私聊）分，也可以按人分。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            try:
                umo = str(event.get_session_id())
            except Exception:
                umo = "unknown"
        if str(self._conf("session_scope", "chat")).lower() == "user":
            return f"{umo}::{_sender_id(event)}"
        return umo

    # ------------------------------------------------------------------ 引擎

    def _bridge_url(self) -> str:
        return str(self._conf("bridge_url", "") or "").strip()

    @property
    def _use_bridge(self) -> bool:
        """配置了 bridge_url 就走桥接模式（AstrBot 与 dsh-go 不在同一环境时用）。"""
        return bool(self._bridge_url())

    def _get_bridge(self) -> BridgeClient:
        url = self._bridge_url()
        if self._bridge is None or self._bridge.url != url:
            self._bridge = BridgeClient(
                url,
                token=str(self._conf("bridge_token", "") or ""),
                timeout=float(self._conf("timeout_seconds", 420)),
                log=self.logger,
            )
        return self._bridge

    async def _ensure_client(self) -> DshAcpClient:
        """拿到可用的引擎连接；必要时拉起子进程。

        启动只在“单飞”任务里做一次，锁内不等待启动过程，
        这样别的聊天发来的指令不会一起卡在一个几十秒的启动上。
        """
        client = self._client
        if client is not None and client.alive:
            return client

        async with self._client_lock:
            # 刚拿到锁，可能别人已经启动好了
            client = self._client
            if client is not None and client.alive:
                return client
            if self._starting is None or self._starting.done():
                self._starting = asyncio.create_task(
                    self._start_client(), name="dsh-go-start"
                )
            starter = self._starting

        try:
            return await asyncio.shield(starter)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 启动失败的 task 会被复用一次是没意义的：清掉引用，
            # 下一条指令重新尝试（真起不来就每次几十秒，但至少有救）。
            self.logger.warning("[dsh-go] 引擎启动失败：%s", e)
            async with self._client_lock:
                if self._starting is starter:
                    self._starting = None
            raise

    async def _start_client(self) -> DshAcpClient:
        """真正去起进程。锁只用来交接指针，耗时的 start() 在锁外做。"""
        async with self._client_lock:
            old, self._client = self._client, None
        if old is not None:
            try:
                await old.stop()
            except Exception:
                self.logger.debug("[dsh-go] 关闭旧引擎出错", exc_info=True)

        cmd = build_command(
            dsh_bin=self._dsh_bin(),
            profile=str(self._conf("profile", "acp")),
            patch_files=self._patch_list(),
            extra_args=self._extra_args(),
        )
        client = DshAcpClient(
            cmd,
            cwd=self._work_dir(),
            env=self._build_env(),
            request_timeout=float(self._conf("timeout_seconds", 420)),
            auto_approve=bool(self._conf("auto_approve", True)),
            log=self.logger,
        )
        self.logger.info("[dsh-go] 启动引擎：%s", " ".join(cmd))
        try:
            await client.start()
        except DshNotRunningError:
            raise
        except Exception as e:
            raise DshError(f"引擎启动失败：{e}") from e

        async with self._client_lock:
            if self._closed:
                # 卸载/关闭已经发生过了，别再复活一个没人回收的引擎
                self._client = None
            else:
                self._client = client
                self._arm_idle_timer()
                return client
        # 走到这里说明插件已关闭：把刚起的进程收掉，别留孤儿
        await client.stop()
        raise DshError("插件已卸载，启动被取消")

    async def _session_for(self, key: str, client: DshAcpClient) -> str:
        """取这个聊天对应的 dsh 会话；能续就续，续不上就新建。"""
        existing = self._sessions.get(key)
        if existing and self._conf("keep_context", True):
            if await client.resume_session(existing):
                await self._apply_model(client, existing)
                return existing
        sid = await client.new_session(self._work_dir())
        self._sessions[key] = sid
        await self._apply_model(client, sid)
        return sid

    async def _apply_model(self, client: DshAcpClient, sid: str) -> None:
        raw = str(self._conf("model", "") or "").strip()
        if not raw or "/" not in raw:
            return
        provider, _, model = raw.partition("/")
        if provider.strip() and model.strip():
            await client.set_model_for(sid, provider.strip(), model.strip())

    def _arm_idle_timer(self) -> None:
        """确保空闲回收守护在跑（幂等）。"""
        limit = float(self._conf("idle_shutdown_minutes", 20) or 0)
        if limit <= 0 or self._closed:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(
            self._idle_watchdog(), name="dsh-go-idle"
        )

    async def _idle_watchdog(self) -> None:
        """每分钟看一眼，闲够了就关引擎。

        用轮询而不是“睡满一段再判断”：任务跨过时间点时下一个周期还能收掉，
        不会因为一次 busy 就永久失去回收能力。
        """
        try:
            while not self._closed:
                await asyncio.sleep(60)
                limit = float(self._conf("idle_shutdown_minutes", 20) or 0)
                if limit <= 0 or self._closed:
                    return
                if await self._reap_if_idle(limit * 60):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("[dsh-go] 空闲回收出错")

    async def _reap_if_idle(self, idle_seconds: float) -> bool:
        """闲够了就关掉引擎，返回是否真的关掉了。

        「检查 + 摘掉引擎」在同一把锁里做，避免刚判断完空闲、
        别的聊天就领了任务、结果引擎被半路关掉。
        """
        async with self._client_lock:
            client = self._client
            if client is None or not client.alive:
                return False
            if self._inflight > 0 or self._busy:
                return False
            if client.idle_seconds() < idle_seconds:
                return False
            self._client = None
            self.logger.info(
                "[dsh-go] 空闲超过 %.0f 分钟，关闭引擎子进程", idle_seconds / 60
            )
        try:
            # 用 shield：万一此时插件正好卸载把这个 reaper 取消，
            # dsh_client 的清理（kill 子进程）也不会半路被打断。
            await asyncio.shield(client.stop())
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.debug("[dsh-go] 空闲关闭引擎出错", exc_info=True)
        return True

    async def _cancel_idle_task(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _shutdown(self) -> None:
        self._closed = True
        await self._cancel_idle_task()
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            try:
                await bridge.close()
            except Exception:
                self.logger.debug("[dsh-go] 关闭桥接客户端出错", exc_info=True)
        starter = self._starting
        self._starting = None
        if starter is not None and not starter.done():
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.stop()
            except Exception:
                self.logger.debug("[dsh-go] 关闭引擎时出错", exc_info=True)

    async def terminate(self) -> None:
        """AstrBot 卸载/停用插件时调用。"""
        await self._shutdown()

    # ------------------------------------------------------------------ 跑一个任务

    async def _run_task(
        self,
        event: AstrMessageEvent,
        text: str,
        key: str,
        progress: bool = True,
    ) -> PromptResult:
        if self._use_bridge:
            return await self._run_task_bridge(text, key)
        client = await self._ensure_client()
        sid = await self._session_for(key, client)

        queue: asyncio.Queue[ToolTrace | None] = asyncio.Queue()
        flusher: asyncio.Task | None = None
        if progress and self._conf("show_progress", True):
            flusher = asyncio.create_task(
                self._flush_progress(event, queue), name="dsh-go-progress"
            )

        def on_tool(trace: ToolTrace) -> None:
            if flusher is not None and trace.status == "in_progress":
                try:
                    queue.put_nowait(trace)
                except Exception:
                    pass

        started = time.time()
        result: PromptResult | None = None
        self._inflight += 1
        try:
            result = await client.prompt(
                sid,
                text,
                timeout=float(self._conf("timeout_seconds", 420)),
                on_tool=on_tool,
            )
        finally:
            self._inflight = max(0, self._inflight - 1)
            if flusher is not None:
                try:
                    queue.put_nowait(None)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(flusher, timeout=5)
                except Exception:
                    flusher.cancel()
        if result is None:  # prompt 抛了非 DshError 的异常，交给上层处理
            raise DshError("dsh-go 没有返回结果（引擎可能已断开）")
        result.elapsed = time.time() - started
        return result

    async def _run_task_bridge(self, text: str, key: str) -> PromptResult:
        """桥接模式：任务交给 dsh-go 那一侧的 dsh_bridge.py 执行。"""
        bridge = self._get_bridge()
        started = time.time()
        self._inflight += 1
        try:
            data = await bridge.ask(text, key)
        except BridgeError as e:
            raise DshError(str(e)) from e
        finally:
            self._inflight = max(0, self._inflight - 1)

        result = data.to_prompt_result(session_id=key)
        if not result.text and result.tools:
            result.text = ""
        result.elapsed = time.time() - started
        self.logger.info(
            "[dsh-go] 桥接完成：ok=%s %.1fs tools=%s",
            data.ok,
            result.elapsed,
            data.tools or "-",
        )
        return result

    async def _flush_progress(
        self, event: AstrMessageEvent, queue: "asyncio.Queue[ToolTrace | None]"
    ) -> None:
        """把 dsh-go 正在用的工具，节流地播报出来。"""
        shown: set[str] = set()
        last = 0.0
        try:
            while True:
                trace = await queue.get()
                if trace is None:
                    return
                title = (trace.title or "工具").strip()
                if not title or title in shown:
                    continue
                now = time.time()
                if now - last < 3.0:
                    continue
                shown.add(title)
                last = now
                await self._send_quiet(event, f"· 正在用 {title}")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.debug("[dsh-go] 进度播报失败", exc_info=True)

    async def _send_quiet(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception:
            self.logger.debug("[dsh-go] 发送进度消息失败", exc_info=True)

    def _format(self, result: PromptResult) -> str:
        limit = int(self._conf("max_reply_chars", 3500) or 3500)
        body = (result.text or "").strip()
        if result.error:
            if result.timed_out:
                hint = f"dsh-go 跑了 {result.elapsed:.0f} 秒还没结束，我先不等了。"
                if body:
                    return f"{body}\n\n（{hint}可以发 /dsh 停止，或把 timeout_seconds 调大）"[:limit]
                return (
                    f"{hint}任务可能还在后台跑，发 /dsh 停止 可以中断；"
                    f"也可以把插件配置 timeout_seconds 调大。"
                )[:limit]
            if body:
                return f"{body}\n\n（dsh-go 没正常收尾：{result.error}）"[:limit]
            return f"dsh-go 这边出错了：{result.error}"[:limit]
        if not body:
            tools = result.tool_summary()
            return (
                "dsh-go 跑完了，但没有文字回复。"
                + (f"\n用到的工具：{tools}" if tools else "")
            )[:limit]
        if len(body) > limit:
            body = body[: max(0, limit - 20)].rstrip() + "\n…（结果太长，已截断）"
        return body

    # ------------------------------------------------------------------ 指令

    @filter.command("dsh", alias={"dshgo"})
    async def dsh_command(self, event: AstrMessageEvent, arg: GreedyStr):
        """把指令交给手机上的 dsh-go 执行。"""
        async for item in self._handle_command(event, str(arg or "")):
            yield item

    async def _handle_command(self, event: AstrMessageEvent, arg: str):
        allowed, reason = self._allow(event)
        if not allowed:
            yield event.plain_result(reason)
            event.stop_event()
            return

        arg = (arg or "").strip()
        fact = arg.lower()

        if not arg or fact in _HELP_FACTS:
            yield event.plain_result(HELP_TEXT)
            event.stop_event()
            return

        key = self._scope_key(event)

        if fact in _STATUS_FACTS:
            yield event.plain_result(await self._status_text())
            event.stop_event()
            return

        if fact in _NEW_FACTS:
            self._sessions.pop(key, None)
            if self._use_bridge:
                try:
                    await self._get_bridge().reset(key)
                except BridgeError as e:
                    yield event.plain_result(f"新建会话失败：{e}")
                    event.stop_event()
                    return
                yield event.plain_result("好，开了个新对话，前面那段上下文不带过去了。")
                event.stop_event()
                return
            if self._client is not None and self._client.alive:
                try:
                    sid = await self._client.new_session(self._work_dir())
                    self._sessions[key] = sid
                    await self._apply_model(self._client, sid)
                except DshError as e:
                    yield event.plain_result(f"新建会话失败：{e}")
                    event.stop_event()
                    return
            yield event.plain_result("好，开了个新对话，前面那段上下文不带过去了。")
            event.stop_event()
            return

        if fact in _STOP_FACTS:
            if self._use_bridge:
                try:
                    stopped = await self._get_bridge().stop(key)
                except BridgeError as e:
                    yield event.plain_result(f"停不了：{e}")
                    event.stop_event()
                    return
                yield event.plain_result(
                    "已经让它停下了。" if stopped else "现在没有在跑的任务。"
                )
                event.stop_event()
                return
            sid = self._sessions.get(key)
            if self._client is not None and sid:
                await self._client.cancel(sid)
                yield event.plain_result("已经让它停下了。")
            else:
                yield event.plain_result("现在没有在跑的任务。")
            event.stop_event()
            return

        if key in self._busy:
            yield event.plain_result("上一条还在跑，等它结束，或者发 /dsh 停止。")
            event.stop_event()
            return

        self._busy.add(key)
        try:
            yield event.plain_result("收到，交给 dsh-go 了…")
            try:
                result = await self._run_task(event, arg, key)
            except DshNotRunningError as e:
                yield event.plain_result(f"连不上手机上的 dsh-go：{e}")
                event.stop_event()
                return
            except DshError as e:
                yield event.plain_result(f"dsh-go 处理失败：{e}")
                event.stop_event()
                return
            except Exception as e:
                self.logger.exception("[dsh-go] 执行任务异常")
                yield event.plain_result(f"dsh-go 这边异常了：{e}")
                event.stop_event()
                return
            yield event.plain_result(self._format(result))
        finally:
            self._busy.discard(key)
        event.stop_event()

    async def _status_text(self) -> str:
        lines = ["dsh-go 状态", ""]
        if self._use_bridge:
            url = self._bridge_url()
            lines.append(f"模式：桥接（{url}）")
            try:
                info = await self._get_bridge().health()
                engine = info.get("engine")
                lines.append(
                    "引擎："
                    + ("已连接" if engine == "connected" else "空闲（下一条指令会自动拉起）")
                )
                lines.append(f"会话数：{info.get('sessions', 0)}")
            except BridgeError as e:
                lines.append(f"桥接服务：连不上（{e}）")
            lines.append(
                f"模型自动调用：{'开' if self._conf('enable_llm_tool', True) else '关'}"
            )
            return "\n".join(lines)

        lines.append("模式：本机")
        client = self._client
        if client is None or not client.alive:
            lines.append("引擎：没启动（下一条指令会自动拉起）")
        else:
            lines.append(f"引擎：已连接，空闲 {client.idle_seconds():.0f} 秒")
            models = client.models
            if models:
                lines.append(f"可选模型：{len(models)} 个")
        lines.append(f"dsh：{self._dsh_bin()}")
        lines.append(f"工作目录：{self._work_dir()}")
        lines.append(f"权限模式：{self._conf('permission_mode', 'danger-full-access')}")
        lines.append(f"记住的会话：{len(self._sessions)} 个")
        lines.append(
            f"模型自动调用：{'开' if self._conf('enable_llm_tool', True) else '关'}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ LLM 工具

    @filter.llm_tool(name="dsh_go")
    async def dsh_go_tool(self, event: AstrMessageEvent, task: str):
        """把任务交给手机上更强的 dsh-go 智能体去执行，用于当前聊天机器人做不到的事：读写文件、执行命令、查网页、看代码仓库。

        Args:
            task(string): 要完整交给 dsh-go 执行的任务，写清目标、涉及的文件路径和约束，它会独立完成后返回结果
        """
        if not self._conf("enable_llm_tool", True):
            return "dsh_go 工具已在插件配置里关闭。"
        allowed, reason = self._allow(event)
        if not allowed:
            return f"无法调用 dsh_go：{reason}"
        task = (task or "").strip()
        if not task:
            return "无法调用 dsh_go：任务内容为空。"

        key = self._scope_key(event)
        if key in self._busy:
            return "dsh-go 正在处理上一条任务，请稍后再调用。"
        self._busy.add(key)
        try:
            result = await self._run_task(event, task, key, progress=False)
        except DshNotRunningError as e:
            return f"连不上手机上的 dsh-go：{e}"
        except DshError as e:
            return f"dsh-go 处理失败：{e}"
        except Exception as e:
            self.logger.exception("[dsh-go] 工具调用异常")
            return f"dsh-go 这边异常了：{e}"
        finally:
            self._busy.discard(key)

        if result.error and not result.text:
            return f"dsh-go 出错了：{result.error}"
        out = (result.text or "").strip() or "（dsh-go 跑完了，但没有给出文字内容）"
        tools = result.tool_summary()
        if tools:
            out += f"\n\n[用到的工具：{tools}]"
        return out

    # ------------------------------------------------------------------ 清理

    @filter.on_plugin_unloaded()
    async def _on_unloaded(self, metadata=None):
        # 只处理自己：别的插件被卸载时不该关掉我的引擎。
        # 用模块路径判断最可靠（实例的 name 由 loader 注入，测试里可能没有）。
        my_module = type(self).__module__
        other_module = getattr(metadata, "module_path", None)
        if other_module and my_module and str(other_module) != my_module:
            return
        other_name = getattr(metadata, "name", None)
        my_name = getattr(self, "name", None)
        if not other_module and other_name and my_name and other_name != my_name:
            return
        await self._shutdown()


def _split_ids(raw: Any) -> set[str]:
    if isinstance(raw, (list, tuple, set)):
        return {str(x).strip() for x in raw if str(x).strip()}
    text = str(raw or "")
    parts = [p.strip() for p in text.replace("\n", ",").replace(" ", ",").split(",")]
    return {p for p in parts if p}


def _sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "")
    except Exception:
        return ""


def _group_id(event: AstrMessageEvent) -> str:
    try:
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return ""
        group = getattr(msg_obj, "group_id", None)
        if group:
            return str(group)
        group_obj = getattr(msg_obj, "group", None)
        return str(getattr(group_obj, "group_id", "") or "")
    except Exception:
        return ""
