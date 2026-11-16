"""调用 dsh-go 桥接服务的客户端。

有些部署里 AstrBot 和 dsh-go 不在同一个 Linux 环境（比如 AstrBot 装在
「泡泡版」自己的容器里，那边没有 dsh）。这时在 dsh-go 一侧运行
``dsh_bridge.py``，AstrBot 这边通过本机回环地址把任务发过去。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

try:  # aiohttp 是 AstrBot 自带依赖，正常情况都在
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

logger = logging.getLogger("astrbot")


class BridgeError(Exception):
    """桥接服务调用失败。"""


@dataclass
class BridgeResult:
    """桥接服务返回的一次结果。"""

    ok: bool = False
    text: str = ""
    error: str = ""
    timed_out: bool = False
    elapsed: float = 0.0
    tools: str = ""

    def to_prompt_result(self, session_id: str = ""):
        """转成插件内部统一的 PromptResult。"""
        from .dsh_client import PromptResult

        return PromptResult(
            text=self.text,
            error=self.error,
            elapsed=self.elapsed,
            session_id=session_id,
            timed_out=self.timed_out,
        )


class BridgeClient:
    """一个很薄的 HTTP 客户端，带连接复用。"""

    def __init__(
        self,
        url: str,
        token: str = "",
        timeout: float = 420.0,
        log: logging.Logger | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.log = log or logger
        self._session = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def _get_session(self):
        if aiohttp is None:
            raise BridgeError(
                "AstrBot 环境里没有 aiohttp，无法使用桥接模式（请改用本机模式）"
            )
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                headers={"X-DSH-Token": self.token} if self.token else {},
            )
        return self._session

    async def _post(self, path: str, payload: dict) -> dict:
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.url}{path}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout + 30),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 401:
                    raise BridgeError(
                        "桥接服务拒绝了 token，请检查插件里的 bridge_token 是否和 dsh_bridge.py 一致"
                    )
                if resp.status == 429:
                    raise BridgeError(body.get("error") or "上一条任务还在跑")
                if resp.status >= 400 and not isinstance(body, dict):
                    raise BridgeError(f"桥接服务返回 {resp.status}")
                return body if isinstance(body, dict) else {}
        except BridgeError:
            raise
        except Exception as e:
            raise BridgeError(
                f"连不上桥接服务 {self.url}（{e}）。"
                "如果手机刚重启过，打开 DSH 的终端敲个回车就能自动拉起，"
                "或运行 sh /root/workspace/dsh-go-bridge/启动.sh"
            ) from e

    async def health(self) -> dict:
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.url}/health", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            raise BridgeError(
                f"连不上桥接服务 {self.url}（{e}）。"
                "打开 DSH 的终端敲个回车即可自动拉起"
            ) from e

    async def ask(self, text: str, session_key: str) -> BridgeResult:
        data = await self._post(
            "/ask", {"text": text, "session": session_key}
        )
        return BridgeResult(
            ok=bool(data.get("ok")),
            text=str(data.get("text") or ""),
            error=str(data.get("error") or ""),
            timed_out=bool(data.get("timed_out")),
            elapsed=float(data.get("elapsed") or 0),
            tools=str(data.get("tools") or ""),
        )

    async def reset(self, session_key: str) -> None:
        await self._post("/reset", {"session": session_key})

    async def stop(self, session_key: str) -> bool:
        data = await self._post("/stop", {"session": session_key})
        return bool(data.get("stopped"))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
