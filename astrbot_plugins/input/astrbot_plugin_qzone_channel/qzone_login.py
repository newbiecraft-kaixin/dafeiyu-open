"""QQ 空间扫码登录：NapCat 拿不到 qzone cookie 时，自己拉一张二维码存到本地，扫完自动写回 manual_cookie。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

import aiohttp

from astrbot.api import logger

from .qzone_config import DATA_ROOT, PLUGIN_DIR

QR_SHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
QR_LOGIN_URL = "https://ptlogin2.qq.com/ptqrlogin"
QR_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# 登录成功后从 cookie 罐里挑出来的键（缺 uin/skey 空间接口必失败）
KEEP_KEYS = ("uin", "skey", "p_skey", "p_uin", "pt2gguin", "RK", "pt4_token", "luin")


def _hash33(text: str) -> int:
    """ptqrtoken 的算法：hash33(qrsig)。"""
    h = 0
    for ch in text:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


class QzoneLoginMixin:
    def _maybe_start_qr_login(self, force: bool = False) -> None:
        """拉起扫码登录；中枢面板主动打开时 force=True，不受 10 分钟节流。"""
        if self._qr_task is not None and not self._qr_task.done():
            return
        now = time.time()
        if not force and now - float(self.state.get("qr_last_ts") or 0) < 600:
            return
        self.state["qr_last_ts"] = now
        self._save_state()
        self._qr_task = asyncio.create_task(self._qr_login_flow())

    async def _watch_scan_request(self) -> None:
        """中枢面板打开「空间动态」时会落一个 scan_request 文件，看到就拉二维码。"""
        flag = os.path.join(DATA_ROOT, "plugin_data", "qzone_login", "scan_request")
        while True:
            try:
                if os.path.exists(flag):
                    os.remove(flag)
                    self._maybe_start_qr_login(force=True)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[qzone_channel] 处理扫码请求失败: {e}")
            await asyncio.sleep(3)

    async def _qr_login_flow(self) -> None:
        try:
            await self._qr_login()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qzone_channel] 扫码登录失败: {type(e).__name__}: {e}")

    def _cookie_string(self, jar) -> str:
        picked: dict[str, str] = {}
        for c in jar:
            if "qq.com" not in str(c["domain"]):
                continue
            if c.key in KEEP_KEYS and c.value:
                picked[c.key] = c.value
        if not (picked.get("p_skey") or picked.get("skey")):
            return ""
        return "; ".join(f"{k}={v}" for k, v in picked.items())

    def _save_cookie(self, cookie: str) -> None:
        self.cfg["manual_cookie"] = cookie
        path = os.path.join(PLUGIN_DIR, "config.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            cfg["manual_cookie"] = cookie
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            logger.info("[qzone_channel] 扫码登录成功，cookie 已写入 config.json")
        except (OSError, ValueError) as e:
            logger.warning(f"[qzone_channel] 写入 cookie 失败: {e}")

    async def _qr_login(self) -> None:
        qr_dir = os.path.join(DATA_ROOT, "plugin_data", "qzone_login")
        os.makedirs(qr_dir, exist_ok=True)
        qr_path = os.path.join(qr_dir, "qr.png")
        headers = {"User-Agent": QR_UA, "Referer": "https://qzone.qq.com/"}
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as sess:
            show_params = {
                "appid": "549000912", "e": "2", "l": "M", "s": "3", "d": "72",
                "v": "4", "t": str(time.time()), "daid": "5", "pt_3rd_aid": "0",
            }
            async with sess.get(QR_SHOW_URL, params=show_params, headers=headers) as r:
                png = await r.read()
            qrsig = next((c.value for c in sess.cookie_jar if c.key == "qrsig"), "")
            if not qrsig or not png:
                raise RuntimeError("拉二维码失败（qrsig/png 为空）")
            with open(qr_path, "wb") as f:
                f.write(png)
            logger.warning(
                f"[qzone_channel] 空间登录态失效，二维码已生成：{qr_path}"
                "（二维码 2 分钟内有效，请自己打开这个文件扫码）"
            )
            token = _hash33(qrsig)
            deadline = time.time() + 180
            while time.time() < deadline:
                await asyncio.sleep(2)
                poll_params = {
                    "u1": "https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone",
                    "ptqrtoken": str(token), "ptredirect": "0", "h": "1", "t": "1",
                    "g": "1", "from_ui": "1", "ptlang": "2052",
                    "action": f"0-0-{int(time.time() * 1000)}", "js_ver": "20102616",
                    "js_type": "1", "login_sig": "", "pt_uistyle": "40",
                    "aid": "549000912", "daid": "5",
                }
                async with sess.get(QR_LOGIN_URL, params=poll_params, headers=headers) as r:
                    text = await r.text()
                m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)'", text)
                if not m:
                    continue
                code, url = m.group(1), m.group(2)
                if code == "65":
                    raise RuntimeError("二维码已过期")
                if code != "0":
                    continue  # 66/67 = 未扫/已扫待确认
                if url:
                    async with sess.get(url, headers=headers) as r2:
                        await r2.read()
                cookie = self._cookie_string(sess.cookie_jar)
                if not cookie:
                    raise RuntimeError("扫码成功但没拿到 skey/p_skey")
                self._save_cookie(cookie)
                logger.info("[qzone_channel] 扫码登录成功，空间回评已恢复")
                return
            raise RuntimeError("二维码超时未确认")
