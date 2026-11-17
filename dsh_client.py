"""dsh-go ACP 客户端。

通过 Agent Client Protocol（JSON-RPC over stdio）驱动 `dsh --profile acp`，
也就是手机上 dsh-go 的本机引擎。

在 AstrBot 里运行时用 `astrbot.api` 的 logger；被桥接服务独立调用时
（那个环境没有 astrbot 包）自动退化为 stderr 输出。

设计要点：
- 常驻一个子进程，避免每条消息冷启动 8~12 秒（实测常驻后 1~3 秒）。
- 每个聊天会话映射一个 DSH 会话，重启进程后用 session/resume 续上。
- 自动应答权限请求，否则 DSH 会一直等审批把任务挂住。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
# 日志：AstrBot 环境里按规范使用 astrbot.api 的 logger；
# 桥接服务独立运行（没有 astrbot 包）时退化为一个极简的 stderr logger。
# 刻意不引用标准库 logging，避免绕过 AstrBot 的日志体系。
try:
    from astrbot.api import logger  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - 仅独立运行时走到

    class _PlainLogger:
        """没有 AstrBot 时的最小 logger，只负责把消息写到 stderr。"""

        @staticmethod
        def _emit(level: str, args: tuple) -> None:
            try:
                sys.stderr.write("[dsh-go] " + level + ": " + " ".join(str(a) for a in args) + "\n")
            except Exception:
                pass

        def debug(self, *args, **kwargs) -> None:  # noqa: D102
            pass

        def info(self, *args, **kwargs) -> None:  # noqa: D102
            self._emit("INFO", args)

        def warning(self, *args, **kwargs) -> None:  # noqa: D102
            self._emit("WARN", args)

        def error(self, *args, **kwargs) -> None:  # noqa: D102
            self._emit("ERROR", args)

        def exception(self, *args, **kwargs) -> None:  # noqa: D102
            self._emit("ERROR", args)

    logger = _PlainLogger()

from dataclasses import dataclass, field
from typing import Any, Callable


PROTOCOL_VERSION = 1


class DshError(Exception):
    """dsh-go 调用失败。"""


class DshNotRunningError(DshError):
    """子进程没起来或已经退出。"""


class DshTimeout(DshError):
    """等 DSH 回话超时。"""

    def __init__(self, message: str, waited: float = 0.0) -> None:
        super().__init__(message)
        self.waited = waited


@dataclass
class ToolTrace:
    """一次任务里 DSH 调用的工具记录，用来看它到底做了什么。"""

    title: str
    kind: str = "other"
    status: str = "in_progress"
    detail: str = ""

    def line(self) -> str:
        name = self.title or "工具"
        if self.status == "failed":
            return f"{name}（失败）"
        if self.status == "completed":
            return name
        return f"{name}（进行中）"


@dataclass
class PromptResult:
    """一次 prompt 的结果。"""

    text: str = ""
    stop_reason: str = ""
    error: str = ""
    tools: list[ToolTrace] = field(default_factory=list)
    elapsed: float = 0.0
    session_id: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and self.stop_reason not in ("refusal", "error")

    def tool_summary(self, limit: int = 6) -> str:
        if not self.tools:
            return ""
        seen: list[str] = []
        for t in self.tools:
            line = t.line()
            if line not in seen:
                seen.append(line)
        if len(seen) > limit:
            seen = seen[:limit] + [f"…等 {len(self.tools)} 次调用"]
        return "、".join(seen)


class DshAcpClient:
    """一个 dsh acp 子进程的连接封装。

    非线程安全，只在同一个事件循环里使用。
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        request_timeout: float = 600.0,
        auto_approve: bool = True,
        log_stderr: bool = True,
        log=None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.request_timeout = request_timeout
        self.auto_approve = auto_approve
        self.log_stderr = log_stderr
        self.log = log or logger

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._listeners: dict[str, Callable[[dict], None]] = {}
        # 本进程里已经打开（新建或恢复）过的会话。重复 resume 一个活着的会话
        # 会被 DSH 拒绝（session is already active），所以要记住。
        self._active_sessions: set[str] = set()
        self._started = False
        self._initialized = False
        self._last_used = 0.0
        self._models: list[tuple[str, str]] = []
        self.last_stderr: list[str] = []

    # ------------------------------------------------------------------ 生命周期

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._initialized
        )

    async def start(self) -> None:
        """启动子进程并完成 initialize 握手。"""
        if self.alive:
            return
        await self._terminate()

        binary = self.command[0]
        if shutil.which(binary) is None and not os.path.exists(binary):
            raise DshNotRunningError(
                f"找不到 dsh 可执行文件：{binary}。请在插件配置里填 dsh_bin 的绝对路径。"
            )

        env = dict(os.environ if self.env is None else self.env)
        env.setdefault("DSH_HOME", os.path.expanduser("~/.dsh"))
        # 子进程不能继承 AstrBot 自己的 asyncio/ssl 相关调优，保持精简即可。
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )
        except FileNotFoundError as e:
            raise DshNotRunningError(f"启动 dsh 失败：{e}") from e

        self._started = True
        self._reader = asyncio.create_task(
            self._read_loop(), name="dsh-acp-reader"
        )
        if self.log_stderr:
            self._stderr_reader = asyncio.create_task(
                self._stderr_loop(), name="dsh-acp-stderr"
            )

        try:
            info = await self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {},
                },
                timeout=120,
            )
        except BaseException:
            # 握手失败就别留一个跑着的孤儿进程
            await self._terminate()
            raise
        self._initialized = True
        self._last_used = time.time()
        self.log.info(
            "[dsh-go] ACP 已连接：%s", (info.get("agentInfo") or {}).get("name", "dsh")
        )

    async def stop(self) -> None:
        await self._terminate()

    async def _terminate(self) -> None:
        """关掉子进程。

        这里要抗取消：AstrBot 卸载插件 / 取消空闲回收时随时可能把这个协程
        取消掉，若清理半路被打断就会留下孤儿 dsh 进程。
        所以真正的清理放在一个独立任务里跑完（用 shield 等它），
        本协程被取消也只会让等待提前结束，不会让清理停下来。
        """
        self._initialized = False
        proc, self._proc = self._proc, None
        reader, self._reader = self._reader, None
        err_reader, self._stderr_reader = self._stderr_reader, None

        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(DshNotRunningError("dsh 进程已退出"))
        self._pending.clear()
        self._active_sessions.clear()

        if proc is None and reader is None and err_reader is None:
            return

        async def cleanup() -> None:
            for task in (reader, err_reader):
                if task is not None and not task.done():
                    task.cancel()
            for task in (reader, err_reader):
                if task is not None:
                    try:
                        await asyncio.gather(task, return_exceptions=True)
                    except BaseException:  # pragma: no cover
                        pass
            if proc is None:
                return
            if proc.returncode is None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except BaseException:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except BaseException:
                    pass
            if proc.returncode is None:
                # 还活着就强杀，必要时直接对 pid 下手
                try:
                    proc.kill()
                except BaseException:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except BaseException:
                    pass
                # 只在这条分支里硬杀 pid：能走到这里说明子进程尚未被回收，
                # 未回收 ⇒ 僵尸 ⇒ 内核不会复用这个 pid，所以 os.kill 不会误杀别人。
                # （别删这个 if：删了就等于拿可能被复用的 pid 去杀进程。）
                if proc.returncode is None:
                    try:
                        os.kill(proc.pid, signal.SIGKILL)
                    except BaseException:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except BaseException:
                        pass

        task = asyncio.create_task(cleanup(), name="dsh-go-cleanup")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 我们被取消了，但清理任务继续跑完（它持着自己的引用）
            self.log.info("[dsh-go] 关闭流程被取消，进程清理仍在后台完成")
            raise
        except Exception:
            self.log.debug("[dsh-go] 关闭子进程时出错", exc_info=True)

    def idle_seconds(self) -> float:
        return time.time() - self._last_used

    # ------------------------------------------------------------------ 底层协议

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            try:
                line = await stdout.readline()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover
                self.log.warning("[dsh-go] 读取 ACP 输出失败：%s", e)
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.log.debug("[dsh-go] 非 JSON 输出：%s", line[:200])
                continue
            await self._dispatch(msg)
        # 进程结束
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(DshNotRunningError("dsh 进程已退出"))
        self._pending.clear()
        self._initialized = False

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while True:
            try:
                line = await stderr.readline()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self.last_stderr.append(text)
            del self.last_stderr[:-40]
            self.log.debug("[dsh-go] %s", text)

    async def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id")
        if msg_id is not None and "method" in msg:
            await self._handle_request(msg_id, msg)
            return
        if msg_id is not None:
            fut = self._pending.pop(msg_id, None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(
                        DshError(
                            f"{err.get('message') or err}"
                            + (
                                f"（{err.get('code')}）"
                                if err.get("code") is not None
                                else ""
                            )
                        )
                    )
                else:
                    fut.set_result(msg.get("result") or {})
            return
        if "method" in msg:
            self._handle_notification(msg)

    async def _handle_request(self, msg_id: Any, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            allow = self.auto_approve
            outcome: dict[str, Any] = (
                {"outcome": "selected", "optionId": "allow-once"}
                if allow
                else {"outcome": "selected", "optionId": "reject-once"}
            )
            self.log.info(
                "[dsh-go] 权限请求：%s → %s",
                ((params.get("toolCall") or {}).get("title") or "工具"),
                "允许" if allow else "拒绝",
            )
            await self._send(
                {"jsonrpc": "2.0", "id": msg_id, "result": {"outcome": outcome}}
            )
            return
        # 其它需要客户端处理的方法一律回“不支持”，避免对面卡死
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"客户端不支持 {method}"},
            }
        )

    def _handle_notification(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/update":
            sid = params.get("sessionId")
            listener = self._listeners.get(sid or "")
            if listener is not None:
                try:
                    listener(params.get("update") or {})
                except Exception:  # pragma: no cover
                    self.log.exception("[dsh-go] 处理 session/update 出错")

    async def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise DshNotRunningError("dsh 进程不可用")
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._write_lock:
            try:
                self._proc.stdin.write(data.encode("utf-8"))
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError) as e:
                raise DshNotRunningError("dsh 进程管道已断开") from e

    async def request(
        self,
        method: str,
        params: dict,
        timeout: float | None = None,
        cancel_session_on_timeout: str | None = None,
    ) -> dict:
        if self._proc is None:
            raise DshNotRunningError("dsh 进程未启动")
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._last_used = time.time()
        # timeout <= 0 表示不限时（写大项目时用）
        effective = self.request_timeout if timeout is None else float(timeout)
        if effective <= 0:
            effective = None
        await self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        try:
            # 用 asyncio.wait 而不是 wait_for：wait_for 超时时会把 future 取消掉，
            # 之后就没法再等它收尾了。
            done, _pending = await asyncio.wait({fut}, timeout=effective)
            if fut in done:
                return fut.result()
            if cancel_session_on_timeout:
                # 任务还挂在 DSH 里。不中断的话这个会话的下一条指令会一直排队
                # （a prompt is already in flight），所以先发取消，再等它收尾。
                await self.cancel(cancel_session_on_timeout)
                done2, _ = await asyncio.wait({fut}, timeout=30)
                if fut in done2:
                    self.log.info("[dsh-go] 超时任务已中断，会话可以继续用")
                else:
                    self.log.warning("[dsh-go] 超时任务 30 秒内没停下，会话可能仍被占用")
            self._pending.pop(req_id, None)
            raise DshTimeout(f"{method} 超时（{(effective or 0):.0f} 秒）", effective or 0)
        finally:
            self._last_used = time.time()

    # ------------------------------------------------------------------ 会话操作

    async def new_session(self, cwd: str | None = None) -> str:
        result = await self.request(
            "session/new", {"cwd": cwd or self.cwd, "mcpServers": []}
        )
        sid = result.get("sessionId")
        if not sid:
            raise DshError("session/new 没有返回 sessionId")
        self._active_sessions.add(sid)
        self._remember_models(result.get("configOptions") or [])
        return sid

    async def resume_session(self, session_id: str, cwd: str | None = None) -> bool:
        """恢复一个持久化的会话；返回是否成功。

        本进程里已经打开过的会话直接复用，不重复 resume
        （DSH 会以 ``session is already active`` 拒绝）。
        """
        if session_id in self._active_sessions:
            return True
        try:
            result = await self.request(
                "session/resume",
                {
                    "sessionId": session_id,
                    "cwd": cwd or self.cwd,
                    "mcpServers": [],
                },
            )
        except DshError as e:
            self.log.info("[dsh-go] 会话 %s 恢复失败：%s", session_id, e)
            return False
        self._active_sessions.add(session_id)
        self._remember_models(result.get("configOptions") or [])
        return True

    async def close_session(self, session_id: str) -> None:
        self._listeners.pop(session_id, None)
        self._active_sessions.discard(session_id)
        try:
            await self.request("session/close", {"sessionId": session_id}, timeout=20)
        except DshError:
            pass

    def _remember_models(self, options: list[dict]) -> None:
        for opt in options:
            if opt.get("id") != "model":
                continue
            found: list[tuple[str, str]] = []
            for group in opt.get("options") or []:
                provider = group.get("group") or ""
                for item in group.get("options") or []:
                    value = item.get("value")
                    model = item.get("name") or ""
                    try:
                        parsed = json.loads(value) if isinstance(value, str) else value
                        if isinstance(parsed, list) and len(parsed) == 2:
                            provider, model = parsed[0], parsed[1]
                    except (json.JSONDecodeError, TypeError):
                        pass
                    found.append((provider, model))
            if found:
                self._models = found

    @property
    def models(self) -> list[tuple[str, str]]:
        """最近一次握手拿到的可选模型列表。"""
        return list(self._models)

    async def set_model_for(self, session_id: str, provider: str, model: str) -> bool:
        if not provider or not model:
            return False
        # 必须用无空格的紧凑 JSON：引擎按字符串精确匹配选项值，
        # json.dumps 默认会输出 '["p", "m"]'（逗号后带空格），匹配不上会报
        # unknown model option。
        value = json.dumps([provider, model], ensure_ascii=False, separators=(",", ":"))
        if self._models and (provider, model) not in self._models:
            self.log.warning(
                "[dsh-go] 模型 %s/%s 不在可选列表里，仍尝试设置", provider, model
            )
        try:
            await self.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": value},
                timeout=60,
            )
            return True
        except DshError as e:
            self.log.warning("[dsh-go] 设置模型失败：%s", e)
            return False

    async def cancel(self, session_id: str) -> None:
        """中断这个会话正在跑的任务。

        ACP 里 ``session/cancel`` 是**通知**（无 id），不是请求：
        发成请求会被对面当成未知方法，任务照跑不停。
        """
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
        except DshNotRunningError as e:
            self.log.info("[dsh-go] 取消任务失败：%s", e)

    async def prompt(
        self,
        session_id: str,
        text: str,
        timeout: float = 600.0,
        on_update: Callable[[dict], None] | None = None,
        on_tool: Callable[[ToolTrace], None] | None = None,
    ) -> PromptResult:
        """发送一条任务并等待完成。"""
        started = time.time()
        chunks: list[str] = []
        tools: dict[str, ToolTrace] = {}

        def listener(update: dict) -> None:
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text":
                    chunks.append(content.get("text") or "")
            elif kind in ("tool_call", "tool_call_update"):
                call_id = update.get("toolCallId") or ""
                trace = tools.get(call_id)
                if trace is None:
                    trace = ToolTrace(title=update.get("title") or "工具")
                    tools[call_id] = trace
                if update.get("title"):
                    trace.title = update["title"]
                if update.get("kind"):
                    trace.kind = update["kind"]
                if update.get("status"):
                    trace.status = update["status"]
                if on_tool is not None:
                    try:
                        on_tool(trace)
                    except Exception:  # pragma: no cover
                        pass
            elif kind == "agent_thought_chunk":
                pass
            if on_update is not None:
                try:
                    on_update(update)
                except Exception:  # pragma: no cover
                    pass

        self._listeners[session_id] = listener
        try:
            result = await self.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=timeout,
                cancel_session_on_timeout=session_id,
            )
        except DshError as e:
            return PromptResult(
                text="".join(chunks).strip(),
                error=str(e),
                tools=list(tools.values()),
                elapsed=time.time() - started,
                session_id=session_id,
                timed_out=isinstance(e, DshTimeout),
            )
        finally:
            self._listeners.pop(session_id, None)

        stop_reason = str(result.get("stopReason") or "")
        text_out = "".join(chunks).strip()
        error = ""
        if stop_reason in ("refusal", "error"):
            error = f"DSH 未完成任务（{stop_reason}）"
        elif stop_reason and stop_reason not in ("end_turn", "stop", "max_turn_requests"):
            # 其它终态（比如 max_tokens）不当作成功，也不静默吞掉
            self.log.warning("[dsh-go] 非预期结束原因：%s", stop_reason)
            if stop_reason == "max_tokens":
                error = "dsh 的输出被长度上限截断了"
        return PromptResult(
            text=text_out,
            stop_reason=stop_reason,
            error=error,
            tools=list(tools.values()),
            elapsed=time.time() - started,
            session_id=session_id,
        )


def build_command(
    dsh_bin: str,
    profile: str = "acp",
    patch_files: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """拼出 `dsh --profile acp [--patch ...]` 命令行。"""
    cmd = [dsh_bin, "--profile", profile]
    for patch in patch_files or []:
        path = os.path.expanduser(patch)
        if path and os.path.exists(path):
            cmd.extend(["--patch", path])
        elif path:
            logger.warning("[dsh-go] 补丁文件不存在，已跳过：%s", path)
    cmd.extend(extra_args or [])
    return cmd
