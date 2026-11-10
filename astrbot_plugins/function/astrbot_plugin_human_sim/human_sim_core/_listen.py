"""私聊记录、监听窗口与拉黑共享存储（从 pipeline.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

from .config import DATA_ROOT

# 拉黑共享存储（basic_tools 写：AI ignore_user tool / 主人 /拉黑 指令）。
# 按 mtime 缓存增量重读：黑名单变更不需要任何插件重载。
_BLOCKLIST_PATH = os.path.join(DATA_ROOT, "blocklist.json")
_BLK_CACHE = {"mtime": 0.0, "items": {}}


def _load_blocklist() -> dict:
    try:
        m = os.path.getmtime(_BLOCKLIST_PATH)
    except OSError:
        return {}
    if m != _BLK_CACHE["mtime"]:
        try:
            with open(_BLOCKLIST_PATH, encoding="utf-8") as f:
                d = json.load(f)
            _BLK_CACHE["items"] = d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            _BLK_CACHE["items"] = {}
        _BLK_CACHE["mtime"] = m
    return _BLK_CACHE["items"]

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class PipelineListenMixin:

    # 监听窗口：主动私聊后 300 秒内，把「这个人」和「原群」当成同一条线。
    _WIN_CACHE = {"ts": 0.0, "items": [], "fetching": False}
    # 续期节流：peer_id -> 上次续期时间
    _RENEW_TS: dict = {}
    # 私聊连发缓冲：umo -> {"texts": [...], "gen": int, "last": float}
    _PM_BUFFER: dict = {}

    async def _debounce_private(self, event, umo: str, text: str) -> bool:
        """等对方把话说完再回答：连发时只有最后一条事件继续走流程。

        每条私聊先记进缓冲区再等「静默窗口」。窗口内又来新消息，本事件直接作废
        （调用方 stop_event），由最后那条把缓冲里的句子合并成一段去问模型，
        这样不会一句一回，也不会漏掉前面的话。

        Returns:
            True 表示本事件是这轮连发的收尾，可以继续处理；False 表示已被后续消息接管。
        """
        window = float(self.cfg.get("private_debounce_seconds", 0) or 0)
        if window <= 0:
            return True
        buf = self._PM_BUFFER.setdefault(umo, {"texts": [], "gen": 0, "last": 0.0})
        buf["gen"] += 1
        my_gen = buf["gen"]
        buf["texts"].append(text)
        buf["last"] = time.time()
        while True:
            await asyncio.sleep(0.2)
            if buf["gen"] != my_gen:
                return False
            if time.time() - buf["last"] >= window:
                break
        texts = [t for t in buf["texts"] if t]
        self._PM_BUFFER.pop(umo, None)
        if len(texts) > 1:
            merged = "\n".join(texts)[:600]
            event.message_str = merged
            logger.info(f"[human_sim] 私聊连发 {len(texts)} 条，合并后回答：{merged[:60]}")
        return True

    def _fetch_listen_windows(self) -> list:
        """拉一次 hub 的监听窗口列表；失败返回空列表。"""
        try:
            import json as _json
            import urllib.request

            with urllib.request.urlopen(
                "http://127.0.0.1:23408/windows", timeout=1.5
            ) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            return ((data or {}).get("data") or {}).get("items") or []
        except Exception:  # noqa: BLE001
            return []

    async def _active_listen_window(self, peer_id: str) -> dict | None:
        """查 hub 的监听窗口；5 秒缓存，hub 不在线就当没有。

        HTTP 刷新放进线程池执行：以前同步 urlopen 直接跑在事件循环里，
        hub 慢/不在线时每条私聊消息都会把整个 bot 卡住最长 1.5 秒。
        """
        now = time.time()
        cache = self._WIN_CACHE
        if now - float(cache.get("ts") or 0) > 5 and not cache.get("fetching"):
            cache["fetching"] = True
            try:
                cache["items"] = await asyncio.to_thread(self._fetch_listen_windows)
                cache["ts"] = time.time()
            finally:
                cache["fetching"] = False
        peer = str(peer_id or "")
        for item in cache.get("items") or []:
            if str(item.get("peer_id") or "") == peer:
                return item
        return None

    def _absorb_listen_window(self, window: dict, text: str, event) -> None:
        """窗口内：私聊内容并入原群上下文，由原群这个会话直接接管回复。"""
        group_umo = str(window.get("group_umo") or "")
        if not group_umo:
            return
        logger.info(
            f"[human_sim] 监听窗口命中：私聊 {window.get('peer_id')} → {group_umo}，由原群会话接管回复"
        )
        # 对方还在接着聊：把窗口续上，别聊到一半监听先过期
        asyncio.create_task(self._renew_listen_window(window))
        sender = str(event.get_sender_name() or window.get("peer_id") or "")
        self._history.setdefault(group_umo, deque(maxlen=30)).append(
            f"{sender}（私聊）：{text[:120]}"
        )
        if not bool(self.cfg.get("listen_window_reply_group", True)):
            return
        private_umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not private_umo:
            return
        peer = str(window.get("peer_id") or "")
        if peer:
            # 对方没加好友时，_send 会按这里记的群走「群临时会话」
            self.state[f"last_group_of:{peer}"] = group_umo
        # 接管：这条私聊不再交给默认流程，否则私聊会回一遍、这里又回一遍
        try:
            event.stop_event()
        except Exception:  # noqa: BLE001
            pass
        asyncio.create_task(self._reply_in_listen_window(group_umo, text, private_umo))

    async def _renew_listen_window(self, window: dict) -> None:
        """对方还在私聊里接着聊 → 给 hub 的监听窗口续期，别让监听中途断掉。

        同一个人 60 秒内只续一次，避免每条私聊都打一次 hub。
        """
        peer = str(window.get("peer_id") or "").strip()
        gid = str(window.get("group_id") or "").strip()
        if not peer or not gid.isdigit():
            return
        now = time.time()
        if now - float(self._RENEW_TS.get(peer) or 0) < 60:
            return
        self._RENEW_TS[peer] = now
        span = int(window.get("seconds") or 0) or 300
        if len(self._RENEW_TS) > 500:
            self._RENEW_TS.clear()
        body = json.dumps(
            {
                "peer_id": peer,
                "group_id": gid,
                "group_umo": str(window.get("group_umo") or ""),
                "seconds": span,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            import urllib.request

            req = urllib.request.Request(
                "http://127.0.0.1:23408/windows/start",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=2)
            # 本地窗口缓存按旧 until 算，立刻作废，下条消息重新拉
            self._WIN_CACHE["ts"] = 0.0
            logger.info(f"[human_sim] 监听窗口续期：私聊 {peer} ↔ 群 {gid}（+{span}s）")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 监听窗口续期失败: {type(e).__name__}: {e}")

    async def _reply_in_listen_window(
        self, group_umo: str, text: str, private_umo: str
    ) -> None:
        """用原群的人设和上下文生成，但把话回到这个私聊窗口。"""
        try:
            prompt = (
                "对方刚在群里和你聊过，现在在私聊里接着说：\n"
                f"{text[:300]}\n"
                "用一句自然的话接住（不要提「私聊」两个字，"
                "也不要解释你是从群里过来的），"
                f"不超过{int(self.cfg.get('max_chars', 60))}字。"
            )
            reply = await self._generate(prompt, group_umo)
            reply = self._humanize(reply)
            if reply:
                await self._send(private_umo, [Plain(reply)])
                self._note_act(group_umo, reply, spoken=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 监听窗口接话失败: {type(e).__name__}: {e}")

    def _blocked_by(self, uid: str) -> dict | None:
        """在拉黑窗口内则返回条目（AI 厌烦拉黑 / 主人 /拉黑），否则 None。"""
        ent = _load_blocklist().get(str(uid or "").strip())
        if ent and float(ent.get("until") or 0) > time.time():
            return ent
        return None

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def _record_private(self, event: AstrMessageEvent):
        """记下谁私聊过我、聊到哪儿了——主动问候 / 话题追问的数据来源。"""
        try:
            blk = self._blocked_by(event.get_sender_id())
            if blk:
                # 厌烦/拉黑窗口内：私聊也不理，直接停事件
                logger.info(
                    f"[human_sim] 已拉黑，忽略私聊（原因：{blk.get('reason') or '没说'}）"
                )
                event.stop_event()
                return
            umo = event.unified_msg_origin
            text = (event.message_str or "").strip()
            qq = str(event.get_sender_id() or "")
            if not text or not qq.isdigit() or qq == str(event.get_self_id() or ""):
                return
            # 戳一戳交给正式会话时 _on_poke 会补一段文本，但那不是真正的私聊发言：
            # 放行给 LLM 即可，别记进主动私聊的话头，也别触发余额/监听窗口那套本地逻辑
            try:
                if any(
                    type(c).__name__ == "Poke"
                    for c in (event.message_obj.message or [])
                ):
                    return
            except Exception:  # noqa: BLE001
                pass
            if text.startswith(("/", "#", "!")):
                return
            # 对方还在补话就先别接：静默窗口内又来新消息时本条作废，
            # 由最后一条把整段话合并后一起回答，避免一句一问一答
            if not await self._debounce_private(event, umo, text):
                event.stop_event()
                return
            # 私聊里问「余额多少」也要本地直答：以前只有群消息走这条，私聊被模型接过去
            # 用自己的 shell 工具瞎猜，最后回一句「摸不着余额」
            if await self._handle_balance_request(event):
                event.stop_event()
                return
            # 监听窗口优先：对方就算没加好友（群临时会话），也由原群会话接管
            window = await self._active_listen_window(qq)
            if window:
                self._absorb_listen_window(window, text, event)
            # 没加好友的陌生人/临时会话不记账：免得之后主动去私聊一个陌生人
            if self._private_is_stranger(event):
                return
            rec = self.state.setdefault("pm_last", {}).setdefault(umo, {})
            rec["qq"] = qq
            rec["ts"] = time.time()
            lines = rec.setdefault("lines", [])
            lines.append(text[:80])
            del lines[:-6]  # 只留最近 6 条，够接话头就行
            # 主动私聊的间隔按最近窗口内的聊天条数算：只留窗口内的，避免数组无限涨
            window = max(1, int(self.cfg.get("pm_active_count_window_days", 3))) * 86400
            cutoff = time.time() - window
            hist = self.state.setdefault("pm_hist", {}).setdefault(umo, [])
            hist.append(time.time())
            self.state["pm_hist"][umo] = [t for t in hist if float(t) >= cutoff]
            # 对方回话了，主动私聊的连发退避清零
            self.state.pop(f"pm_miss:{umo}", None)
            self._save_state_soon()  # 高频路径：防抖落盘，别每条私聊都全量写盘
        except Exception:  # noqa: BLE001
            pass
