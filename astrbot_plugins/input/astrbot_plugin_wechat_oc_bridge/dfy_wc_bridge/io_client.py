"""IO 层：封装 io-wechat 的 /messages 与 /send（仅文字）。"""

from __future__ import annotations

import httpx

from astrbot.api import logger


def _json(resp) -> dict:
    """io-wechat 失败时是 5xx，但正文里有真实原因，不要直接丢掉。"""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _err(data: dict):
    return data.get("error") or data.get("message") or data or "未知错误"


class IoWechatClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=60.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def messages(self, since: int, wait_ms: int = 0) -> dict:
        params = {"since": since}
        if wait_ms:
            params["wait"] = wait_ms
        resp = await self._client.get(f"{self.base}/messages", params=params)
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, to: str, text: str) -> bool:
        payload = {"to": to, "text": text}
        try:
            resp = await self._client.post(
                f"{self.base}/send",
                json=payload,
                timeout=httpx.Timeout(120.0, read=120.0),
            )
            data = _json(resp)
            if not data.get("ok"):
                logger.warning(f"[wechat_bridge] 发送失败 to={to} [{resp.status_code}]: {_err(data)}")
                return False
            logger.info(f"[wechat_bridge] 已发送 -> {to}: {text[:40]}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 发送异常 to={to}: {exc}")
            return False
