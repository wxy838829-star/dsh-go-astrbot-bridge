#!/usr/bin/env python3
"""dsh-go 桥接服务。

跑在 dsh-go 这一侧的 Linux 环境里，把 AstrBot 插件发来的任务
交给本机的 dsh 引擎执行，再把结果用 HTTP 返回。

为什么需要它：AstrBot 泡泡版装在自己的 Linux 容器里，那边没有 dsh。
两个容器共享同一个网络栈，所以 AstrBot 能通过 127.0.0.1 找到这里。

用法：
    python3 dsh_bridge.py                 # 默认监听 127.0.0.1:6799
    python3 dsh_bridge.py --port 6799
    python3 dsh_bridge.py --token 我的口令
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsh_client import (  # noqa: E402
    DshAcpClient,
    DshError,
    DshNotRunningError,
    build_command,
)

LOG = logging.getLogger("dsh-bridge")

DEFAULT_PORT = 6799
DEFAULT_CONFIG = {
    "dsh_bin": "dsh",
    "profile": "acp",
    "patch_files": ["~/.dsh/profiles/web/cordis.patch.yml"],
    "extra_args": [],
    "work_dir": "~",
    "permission_mode": "danger-full-access",
    "env_api_keys": "",
    "model": "",
    "auto_approve": True,
    "timeout_seconds": 420,
    # /download 只允许这些目录下的文件（防止被当成任意文件读取器）
    "download_roots": ["/root/workspace", "/sdcard/Download/DSH-Go", "/tmp"],
    "download_max_mb": 20,
}


def build_env(cfg: dict) -> dict[str, str]:
    env = dict(os.environ)
    mode = str(cfg.get("permission_mode") or "").strip()
    if mode:
        env["DSH_PERMISSION_MODE"] = mode
    for line in str(cfg.get("env_api_keys") or "").replace(",", "\n").splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() and v.strip():
                env[k.strip()] = v.strip()
    return env


class Bridge:
    """持有一个常驻的 dsh 引擎连接。"""

    def __init__(self, cfg: dict, token: str, idle_minutes: float = 30) -> None:
        self.cfg = cfg
        self.token = token
        self.idle_minutes = idle_minutes
        self.client: DshAcpClient | None = None
        self.lock = asyncio.Lock()
        self.sessions: dict[str, str] = {}
        self.busy: dict[str, float] = {}  # 聊天键 -> 开始时间，用于识别僵死占用
        self.prompts: dict[str, asyncio.Task] = {}  # 聊天键 -> 正在跑的请求任务
        self.inflight = 0
        self.started_at = time.time()
        self._idle_task: asyncio.Task | None = None

    # -------------------------------------------------------------- 引擎

    async def ensure_client(self) -> DshAcpClient:
        if self.client is not None and self.client.alive:
            return self.client
        async with self.lock:
            if self.client is not None and self.client.alive:
                return self.client
            cmd = build_command(
                dsh_bin=str(self.cfg.get("dsh_bin") or "dsh"),
                profile=str(self.cfg.get("profile") or "acp"),
                patch_files=[os.path.expanduser(p) for p in self.cfg.get("patch_files") or []],
                extra_args=list(self.cfg.get("extra_args") or []),
            )
            client = DshAcpClient(
                cmd,
                cwd=os.path.expanduser(str(self.cfg.get("work_dir") or "~")),
                env=build_env(self.cfg),
                request_timeout=float(self.cfg.get("timeout_seconds") or 420),
                auto_approve=bool(self.cfg.get("auto_approve", True)),
                log=LOG,
            )
            LOG.info("启动引擎：%s", " ".join(cmd))
            await client.start()
            self.client = client
            self.sessions.clear()
            self.arm_idle()
            return client

    def arm_idle(self) -> None:
        if self.idle_minutes <= 0:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self.inflight or self.busy or self.client is None:
                continue
            if self.client.idle_seconds() < self.idle_minutes * 60:
                continue
            LOG.info("空闲超时，关闭引擎")
            client, self.client = self.client, None
            try:
                await asyncio.shield(client.stop())
            except Exception:
                LOG.debug("空闲关闭出错", exc_info=True)
            return

    async def session_for(self, key: str, client: DshAcpClient) -> str:
        sid = self.sessions.get(key)
        if sid and await client.resume_session(sid):
            return sid
        sid = await client.new_session(os.path.expanduser(str(self.cfg.get("work_dir") or "~")))
        self.sessions[key] = sid
        raw = str(self.cfg.get("model") or "").strip()
        if "/" in raw:
            provider, _, model = raw.partition("/")
            await client.set_model_for(sid, provider.strip(), model.strip())
        return sid

    async def ask(self, text: str, key: str, timeout: float | None = None) -> dict:
        client = await self.ensure_client()
        sid = await self.session_for(key, client)
        self.inflight += 1
        try:
            started = time.time()
            raw_timeout = (
                timeout if timeout is not None else self.cfg.get("timeout_seconds", 420)
            )
            task_timeout = float(raw_timeout if raw_timeout is not None else 420)
            if task_timeout <= 0:
                task_timeout = None  # 不限时：写大项目时用
            result = await client.prompt(sid, text, timeout=task_timeout)
            return {
                "ok": result.ok,
                "text": result.text,
                "error": result.error,
                "timed_out": result.timed_out,
                "elapsed": round(time.time() - started, 1),
                "tools": result.tool_summary(),
            }
        except asyncio.CancelledError:
            # 调用方（插件）断开了：如果不在服务端把 DSH 那边的任务停掉，
            # 这个会话会一直被占着，后续请求都会收到「上一条任务还在跑」。
            try:
                await asyncio.shield(client.cancel(sid))
                LOG.info("调用方断开，已中断会话 %s 的任务", sid)
            except Exception:
                LOG.debug("断开后取消任务失败", exc_info=True)
            raise
        finally:
            self.inflight = max(0, self.inflight - 1)

    async def close(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        client, self.client = self.client, None
        if client is not None:
            await client.stop()


# ------------------------------------------------------------------ HTTP


def make_app(bridge: Bridge) -> web.Application:
    async def health(request: web.Request) -> web.Response:
        client = bridge.client
        return web.json_response(
            {
                "ok": True,
                "service": "dsh-go bridge",
                "uptime": round(time.time() - bridge.started_at),
                "engine": "connected" if (client and client.alive) else "idle",
                "sessions": len(bridge.sessions),
                "inflight": bridge.inflight,
            }
        )

    async def ask(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)
        text = str(body.get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "text 不能为空"}, status=400)
        key = str(body.get("session") or "default")
        req_timeout = body.get("timeout", None)
        if req_timeout is not None:
            try:
                req_timeout = float(req_timeout)
            except (TypeError, ValueError):
                req_timeout = None
        # 占用超过上限时间的一律视为僵死（比如上一次进程被强杀），直接清掉
        started_at = bridge.busy.get(key)
        if started_at is not None and time.time() - started_at > max(
            3600.0, float(bridge.cfg.get("timeout_seconds", 420) or 420) + 600
        ):
            bridge.busy.pop(key, None)
            LOG.warning("清理僵死的会话占用：%s", key)
        if key in bridge.busy:
            return web.json_response({"ok": False, "error": "上一条任务还在跑"}, status=429)
        bridge.busy[key] = time.time()
        # 包成独立 task，/stop 就能精确取消这一次请求（哪怕引擎还在启动）
        task = asyncio.create_task(bridge.ask(text, key, timeout=req_timeout))
        bridge.prompts[key] = task
        try:
            data = await task
            return web.json_response(data)
        except DshNotRunningError as e:
            return web.json_response({"ok": False, "error": f"连不上 dsh：{e}"}, status=503)
        except DshError as e:
            return web.json_response({"ok": False, "error": f"dsh 处理失败：{e}"}, status=502)
        except Exception as e:  # pragma: no cover
            LOG.exception("处理任务异常")
            return web.json_response({"ok": False, "error": f"内部错误：{e}"}, status=500)
        finally:
            bridge.prompts.pop(key, None)
            bridge.busy.pop(key, None)

    async def reset(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        key = str((body or {}).get("session") or "default")
        bridge.sessions.pop(key, None)
        return web.json_response({"ok": True})

    async def stop(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        key = str((body or {}).get("session") or "default")

        sid = bridge.sessions.get(key)
        task = bridge.prompts.get(key)
        did = False

        # 先让 DSH 那边停下（有会话才发得出去）
        if bridge.client is not None and sid:
            try:
                await asyncio.shield(bridge.client.cancel(sid))
                did = True
            except Exception:
                LOG.debug("停止时取消 DSH 任务失败", exc_info=True)

        # 再掐断这次请求本身，让 finally 把占用清掉
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            did = True

        # 兜底：不管上面成没成，占用标记一定清掉
        bridge.prompts.pop(key, None)
        bridge.busy.pop(key, None)
        return web.json_response({"ok": True, "stopped": did})

    async def download(request: web.Request) -> web.Response:
        raw = str(request.query.get("path") or "").strip()
        if not raw:
            return web.json_response({"ok": False, "error": "缺少 path 参数"}, status=400)
        target = os.path.realpath(os.path.expanduser(raw))
        roots = [os.path.realpath(os.path.expanduser(r)) for r in bridge.cfg.get("download_roots") or []]
        if not any(target == root or target.startswith(root + os.sep) for root in roots):
            return web.json_response(
                {"ok": False, "error": f"不允许下载白名单外的路径：{target}"}, status=403
            )
        if not os.path.isfile(target):
            return web.json_response({"ok": False, "error": "文件不存在"}, status=404)
        max_bytes = int(float(bridge.cfg.get("download_max_mb") or 20) * 1024 * 1024)
        try:
            size = os.path.getsize(target)
        except OSError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        if size > max_bytes:
            return web.json_response(
                {"ok": False, "error": f"文件过大（{size} 字节，上限 {max_bytes}）"}, status=413
            )
        return web.FileResponse(
            target,
            headers={
                "Content-Disposition": f'attachment; filename="{os.path.basename(target)}"'
            },
        )

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_route("*", "/health", _guard(health, bridge))
    app.router.add_route("*", "/ask", _guard(ask, bridge))
    app.router.add_route("*", "/reset", _guard(reset, bridge))
    app.router.add_route("*", "/stop", _guard(stop, bridge))
    app.router.add_route("*", "/download", _guard(download, bridge))
    return app


def _guard(handler, bridge: Bridge):
    async def wrapper(request: web.Request) -> web.Response:
        token = request.headers.get("X-DSH-Token") or request.query.get("token") or ""
        if bridge.token and not secrets.compare_digest(token, bridge.token):
            return web.json_response({"ok": False, "error": "token 不对"}, status=401)
        return await handler(request)

    return wrapper


async def main() -> None:
    parser = argparse.ArgumentParser(description="dsh-go 桥接服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=os.environ.get("DSH_BRIDGE_TOKEN", ""))
    parser.add_argument("--config", default="", help="JSON 配置文件，可覆盖默认配置")
    parser.add_argument("--idle-minutes", type=float, default=30)
    parser.add_argument(
        "--task-timeout",
        type=float,
        default=None,
        help="单个任务超时秒数，0 表示不限时（默认取配置文件里的 timeout_seconds）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = dict(DEFAULT_CONFIG)
    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if args.task_timeout is not None:
        cfg["timeout_seconds"] = args.task_timeout

    token = args.token
    if not token:
        token = secrets.token_urlsafe(18)
        LOG.warning("未指定 token，本次自动生成：%s", token)

    bridge = Bridge(cfg, token, idle_minutes=args.idle_minutes)
    app = make_app(bridge)

    # handler_cancellation: 客户端断开时取消处理函数。
    # 不加这个，调用方断开后服务端仍会把任务跑完、会话一直被占着。
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    LOG.info("桥接服务已启动：http://%s:%s", args.host, args.port)
    LOG.info("token：%s", token)
    LOG.info("健康检查：curl -s http://127.0.0.1:%s/health", args.port)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bridge.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
