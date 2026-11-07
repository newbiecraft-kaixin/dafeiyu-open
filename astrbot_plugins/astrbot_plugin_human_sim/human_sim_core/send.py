"""发送与消息表情回应：出站发送、贴表情、跟贴与回贴。"""

from __future__ import annotations

import asyncio
import base64
import random
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from urllib.request import url2pathname
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    Face,
    File,
    Forward,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)

from .config import FACE_REFRESH_SECONDS, REACTION_IDS
from .text import _REACT_ACTION_RE

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


def _emoji_like_error(resp) -> str:
    """NapCat 会返回 retcode=0 但 data.result!=0，必须看 errMsg 才知道是否真贴上。"""
    data = resp
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if not isinstance(data, dict):
        return ""
    err = str(data.get("errMsg") or "").strip()
    if err:
        return err
    result = data.get("result")
    if result not in (None, 0, "0"):
        return f"result={result}"
    return ""


class SendMixin:

    # ---------- 统一发送（必须带 self_id 路由） ----------
    async def _send(self, umo: str, chain: list) -> bool:
        """主动发消息：优先带 self_id 直连 OneBot action。
        context.send_message 走 send_by_session(event=None) 时不带 self_id，
        一旦有第二个连接（测试桥/多账号）就 ApiNotAvailable——自主回话会全静默。
        返回 True=发出去了，False=两条通道都失败。
        """
        # 群禁言期间所有主动发言直接丢弃（/解禁 之前保持安静）
        if ":GroupMessage:" in umo and self._is_muted(umo):
            logger.info(f"[human_sim] 群禁言中，丢弃发送（{umo.rsplit(':', 1)[-1]}）")
            return False
        # 防「连说两句」：刚开过口（含主流水线回复）就压住这条主动发言
        try:
            gap = float(self.cfg.get("send_min_gap_seconds", 8))
        except (TypeError, ValueError):
            gap = 8.0
        if gap > 0:
            last = float(self.state.get(f"last_bot_say:{umo}", 0) or 0)
            if last and time.time() - last < gap:
                logger.info(
                    f"[human_sim] 刚开过口 {time.time() - last:.1f}s，压住连发"
                    f"（{umo.rsplit(':', 1)[-1]}）"
                )
                return False
        # 跨机器人共享分钟额度：同群这一分钟已经发满就不再开口
        if not self._shared_claim(umo):
            logger.info(
                f"[human_sim] 同群本分钟发言已达上限"
                f"（{self.cfg.get('shared_minute_cap', 20)} 条），丢弃发送"
            )
            return False
        # 输出层统一收口：:名称: 换成真图，认不出的直接抹掉，绝不把裸占位符发出去
        chain = self._render_placeholders(chain)
        if not chain:
            return False
        # 出站节流：同群/同会话排队，保证和上一条至少隔 1.6~3 秒，避免同一秒连发被风控
        if self._gate is not None:
            await self._gate.wait(umo)
        bot = self._bots.get(umo)
        gid = umo.rsplit(":", 1)[-1]
        is_group = ":GroupMessage:" in umo
        if bot is not None and gid.isdigit():
            segs: list[dict] = []
            for comp in chain:
                if isinstance(comp, Plain):
                    segs.append({"type": "text", "data": {"text": comp.text}})
                elif isinstance(comp, At):
                    segs.append({"type": "at", "data": {"qq": str(comp.qq)}})
                elif isinstance(comp, Face):
                    segs.append({"type": "face", "data": {"id": str(comp.id)}})
                elif isinstance(comp, Image):
                    path = getattr(comp, "file", None) or ""
                    if path.startswith("file://"):
                        # urlparse().path 在 Windows 上给出 /C:/... —— 本地 open
                        # 和 NapCat（识别URL失败 retcode=1200）都认不出这个格式，
                        # 必须用 url2pathname 转回 C:\...，否则表情永远发不出去
                        path = url2pathname(urlparse(path).path)
                    blob = None
                    try:
                        with open(path, "rb") as f:
                            blob = f.read()
                    except (OSError, ValueError):
                        # 打不开不代表发不了：别人发来的图，comp.file 往往只是 NapCat
                        # 缓存里的文件名（或已过期的下载链接），NapCat 自己认得，
                        # 原样交给它就行。以前这里直接 continue，导致「文字+图片」的
                        # 复读 / +1 只剩文字发出去 —— 看起来就像根本没跟上。
                        raw = path or str(getattr(comp, "url", "") or "")
                        if raw:
                            segs.append(
                                {
                                    "type": "image",
                                    "data": {
                                        "file": raw,
                                        "sub_type": 1,
                                        "summary": "[动画表情]",
                                    },
                                }
                            )
                        continue
                    segs.append(
                        {
                            "type": "image",
                            "data": {
                                "file": "base64://" + base64.b64encode(blob).decode(),
                                # sub_type=1 → NapCat 映射成 picSubType，QQ 才会当表情渲染
                                "sub_type": 1,
                                "summary": "[动画表情]",
                            },
                        }
                    )
            if segs:
                params: dict = {"message": segs}
                params["group_id" if is_group else "user_id"] = int(gid)
                if not is_group and not await self._is_friend(bot, gid):
                    # 对方没加好友（多半是从群里点的临时会话）：带上他最近待的群，
                    # OneBot 会按「群临时会话」发出去，不会报「请先添加对方为好友」
                    last_g = str(self.state.get(f"last_group_of:{gid}", "") or "")
                    lg = last_g.rsplit(":", 1)[-1]
                    if lg.isdigit():
                        params["group_id"] = int(lg)
                        logger.info(f"[human_sim] {gid} 非好友，改走群临时会话（群 {lg}）")
                sid = str(self.state.get(f"self_id:{umo}", "") or "")
                if sid.isdigit():
                    params["self_id"] = int(sid)
                try:
                    result = await bot.api.call_action(
                        "send_group_msg" if is_group else "send_private_msg", **params
                    )
                    self._track_own_msg(umo, result)
                    # 记「上次开口时间」：沉默补偿（_ramp_probability）按它算
                    self.state[f"last_bot_say:{umo}"] = datetime.now().timestamp()
                    if self._gate is not None:
                        self._gate.mark(umo)
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[human_sim] 直连发送失败，回退 context: {type(e).__name__}: {e}"
                    )
        try:
            await self.context.send_message(umo, MessageChain(chain=chain))  # 兜底（仅单端可用）
        except Exception as e:  # noqa: BLE001
            # 发不出去（非好友 / 群已解散 / 被限流）只记一条，绝不把异常抛给事件流水线
            logger.warning(f"[human_sim] 兜底发送失败（{umo}）: {type(e).__name__}: {e}")
            self.state[f"last_bot_say:{umo}"] = datetime.now().timestamp()
            return False
        self.state[f"last_bot_say:{umo}"] = datetime.now().timestamp()
        if self._gate is not None:
            self._gate.mark(umo)
        return True

    # ---------- 消息表情回应（在已发言下面挂 QQ 表情） ----------
    def _pick_reaction(self, umo: str, pool) -> int:
        """同群轮换贴表情：只避开最近贴过的那几个，剩下的照旧随机，别每次都是 14/21。"""
        cands = [int(x) for x in pool] if pool else list(REACTION_IDS)
        if not cands:
            return 14
        recent = self._react_recent.setdefault(umo, [])
        keep = max(1, len(cands) - 3)          # 只冷藏最近 3 个，保留随机手感
        fresh = [e for e in cands if e not in recent]
        pick = random.choice(fresh or cands)
        recent.append(pick)
        del recent[:-keep]
        return pick

    def _face_name_map(self) -> dict[int, str]:
        """官方黄脸 id → 名称（不含超级表情），给模型列候选用。"""
        cache = getattr(self, "_face_name_cache", None)
        if cache is not None and time.time() - float(
            getattr(self, "_face_cfg_ts", 0)
        ) < FACE_REFRESH_SECONDS:
            return cache
        names: dict[int, str] = {}
        for r in self._read_face_config():
            if r.get("AniStickerType"):
                continue
            sid = str(r.get("QSid") or "").strip()
            name = str(r.get("QDes") or "").lstrip("/").strip()
            if sid.isdigit() and name:
                names[int(sid)] = name
        self._face_name_cache = names
        return names

    def _reaction_menu(self, umo: str) -> list[tuple[int, str]]:
        """把候选表情一次列全给模型（最近贴过的排后面），贴不贴、贴哪个由模型定。"""
        pool = [int(x) for x in (self.cfg.get("reaction_ids") or REACTION_IDS)]
        names = self._face_name_map()
        cands = [(i, names.get(i, f"表情{i}")) for i in pool]
        recent = set(self._react_recent.get(umo, []))
        cands.sort(key=lambda c: c[0] in recent)
        k = max(1, int(self.cfg.get("react_ai_candidates", 10)))
        return cands[:k]

    def _resolve_reaction(self, raw: str) -> int | None:
        """模型写的表情名/id → face id；认不出来返回 None。"""
        raw = (raw or "").strip().strip("“”\"'")
        if raw.isdigit():
            return int(raw)
        m = re.fullmatch(r"表情(\d+)", raw)
        if m:
            return int(m.group(1))
        names = self._face_name_map()
        for fid, name in names.items():
            if name == raw:
                return fid
        for fid, name in names.items():
            if raw and (raw in name or name in raw):
                return fid
        return None

    def _extract_react_action(self, reply: str) -> tuple[str, int | None]:
        """从模型回复里剥出 REACT(...) 动作，返回（正文, face id）。"""
        m = _REACT_ACTION_RE.search(reply or "")
        if not m:
            return reply, None
        fid = self._resolve_reaction(m.group(1))
        rest = (reply[:m.start()] + reply[m.end():]).strip()
        return rest, fid

    async def _react(
        self, umo: str, message_id, emoji_id: int | None = None,
        force: bool = False, note: bool = True,
    ) -> bool:
        """对一条已发送的消息贴表情回应（NapCat 扩展 API set_msg_emoji_like）"""
        if message_id in (None, "", 0):
            return False
        if self._is_private(umo):
            # QQ 私聊的 set_msg_emoji_like 恒被拒（result=65011），别浪费调用也别记成已贴
            logger.debug(f"[human_sim] 私聊不支持表情回应，跳过（{umo}）")
            return False
        now = time.time()
        if not force and now - self._react_last.get(umo, 0) < float(
            self.cfg.get("react_cooldown_seconds", 120)
        ):
            return False
        bot = self._bots.get(umo)
        if bot is None:
            return False
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            mid = str(message_id)
        pool = self.cfg.get("reaction_ids") or REACTION_IDS
        # 多客户端连接时必须带 self_id 才能路由到正确的 OneBot 连接（api_impl.py:131）
        call_kwargs = {
            "message_id": mid,
            "emoji_id": str(emoji_id if emoji_id is not None else self._pick_reaction(umo, pool)),
            "set": True,
        }
        sid = str(self.state.get(f"self_id:{umo}") or "")
        if sid:
            call_kwargs["self_id"] = sid
        try:
            resp = await bot.api.call_action("set_msg_emoji_like", **call_kwargs)
            err = _emoji_like_error(resp)
            if err:
                logger.warning(
                    f"[human_sim] 表情回应失败（emoji:{call_kwargs.get('emoji_id')} 消息 {mid}）: {err}"
                )
                return False
            self._react_last[umo] = now
            eid = str(call_kwargs.get("emoji_id") or "")
            try:
                ename = self._face_name_map().get(int(eid)) if eid.isdigit() else None
            except Exception:  # noqa: BLE001
                ename = None
            if note:
                self._note_act(umo, f"在消息下标了个表情{'：' + ename if ename else ''}")
            logger.info(f"[human_sim] 表情回应 -> 群 {umo.rsplit(':', 1)[-1]} 消息 {mid}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 表情回应失败: {type(e).__name__}: {e}")
            return False

    async def _delayed_react(
        self, umo: str, message_id, delay: float, emoji_id: int | None = None
    ) -> None:
        """延迟几秒再贴表情——真人不会秒贴"""
        await asyncio.sleep(delay)
        try:
            await self._react(umo, message_id, emoji_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 延迟表情回应失败: {type(e).__name__}: {e}")

    # ---------- 接收用户在 AI 消息下贴的表情回应 ----------
    def _track_own_msg(self, umo: str, result) -> None:
        """记录机器人自己发出过的 message_id（表情回应通知不带发送者，靠这个命中）。"""
        if result is None:
            return
        mid = None
        if isinstance(result, dict):
            mid = result.get("message_id") or (result.get("data") or {}).get("message_id")
        elif isinstance(result, int):
            mid = result
        if mid is None:
            return
        s = self._own_msg_ids.setdefault(umo, set())
        s.add(int(mid))
        if len(s) > 800:  # 上限保护，丢掉一半避免无限增长
            for _ in range(len(s) // 2):
                s.pop()

    async def _msg_sender(self, umo: str, message_id):
        """get_msg 反查消息发送者；拿不到返回 None（不影响主链路）。"""
        bot = self._bots.get(umo)
        if bot is None:
            return None
        try:
            ret = await bot.api.call_action("get_msg", message_id=int(message_id))
            if isinstance(ret, dict):
                data = ret.get("data", ret)
                if isinstance(data, dict):
                    sender = (data.get("sender") or {}).get("user_id")
                    if sender is None:
                        sender = data.get("user_id")
                    return sender
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] get_msg 失败 mid={message_id}: {type(e).__name__}: {e}")
        return None

    async def _on_emoji_like(self, umo: str, raw: dict) -> None:
        """收到用户在消息下贴的表情回应（NapCat 扩展 notice）。

        通知里没有「被回应消息的发送者」，判定是不是 AI 自己的消息靠两点：
          1) 快速命中：human_sim 自己发过的 message_id（_own_msg_ids）
          2) 命中不了就 get_msg 反查 sender，等于 self_id 才算
        只处理「用户对你自己消息的反应」，别人的互表情不凑热闹。
        """
        try:
            self_id = str(
                raw.get("self_id")
                or self.state.get(f"self_id:{umo}", "")
                or ""
            )
            reactor = str(raw.get("user_id") or "")
            if reactor == self_id:
                return  # 机器人自己贴的回应回显，忽略，避免对刷
            mid = raw.get("message_id")
            gid = str(raw.get("group_id") or "")
            likes = raw.get("likes") or []
            emoji_ids = [
                str(x.get("emoji_id"))
                for x in likes
                if isinstance(x, dict) and x.get("emoji_id") is not None
            ]
            is_add = raw.get("is_add", True)
            if not mid or not emoji_ids:
                return
            # 判定是不是机器人自己的消息
            own_set = self._own_msg_ids.get(umo, set())
            if str(mid).isdigit() and int(mid) in own_set:
                own = True
            elif mid in own_set:
                own = True
            else:
                sender = await self._msg_sender(umo, mid)
                own = sender is not None and str(sender) == str(self_id)
            if not own:
                # 输入层 +1：别人在同一条消息下连贴同一个表情，跟着贴一个
                if is_add and self.cfg.get("reaction_plus1", True):
                    await self._reaction_plus1(umo, mid, emoji_ids, reactor)
                return
            # —— 是 AI 自己的消息，记录 + 互动 ——
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rec = {
                "uid": reactor,
                "emoji_ids": emoji_ids,
                "mid": mid,
                "is_add": bool(is_add),
                "ts": ts,
            }
            lst = self.state.setdefault(f"reactions:{umo}", [])
            lst.append(rec)
            if len(lst) > 200:
                del lst[: len(lst) - 200]
            self._reactions_seen += 1
            logger.info(
                f"[human_sim] 收到表情回应 -> 群 {gid} 用户 {reactor} 对消息 {mid} "
                f"贴了 {emoji_ids}（{'加' if is_add else '取消'}）"
            )
            # 根据情况回复：加表情时默认回贴一个（对称互动，感知被回应）；冷却内不重复贴
            if is_add and self.cfg.get("react_back_on_reaction", True):
                if time.time() - self._react_last.get(umo, 0) >= float(
                    self.cfg.get("react_cooldown_seconds", 120)
                ):
                    await self._react(umo, mid, force=True)
            # 用了机器人自己的反应池表情（明确正/负向信号）时，小概率额外文字互动
            if self.cfg.get("react_back_text_probability", 0) and random.random() < float(
                self.cfg.get("react_back_text_probability", 0)
            ):
                pool = set(int(x) for x in (self.cfg.get("reaction_ids") or []))
                if any(str(e).isdigit() and int(e) in pool for e in emoji_ids):
                    await self._ack_reaction(umo, reactor)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 表情回应处理异常: {type(e).__name__}: {e}")

    async def _reaction_plus1(
        self, umo: str, mid, emoji_ids: list[str], reactor: str
    ) -> None:
        """输入层 +1：两个人给同一条消息连贴同一个表情，跟着贴同一个（一条只跟一次）。"""
        if not emoji_ids or not mid:
            return
        now = time.time()
        key = f"{umo}|{mid}"
        window = float(self.cfg.get("plus1_window_seconds", 300))
        last = self._react_chain.get(key)
        self._react_chain[key] = (list(emoji_ids), now)
        # 按消息计数会一直涨：超量就清最老的，避免长跑内存里堆垃圾
        if len(self._react_chain) > 500:
            self._react_chain.clear()
            self._react_join_at.clear()
        # 「上一下」也在窗口内，且撞上同一个表情，才算接龙；不同表情只是各自回应
        if not last or now - last[1] > window:
            return
        same = set(last[0]) & set(emoji_ids)
        if not same:
            return
        if now - self._react_join_at.get(key, 0) < float(
            self.cfg.get("reaction_plus1_cooldown_seconds", 60)
        ):
            return
        emoji = next(iter(same))
        ok = await self._react(
            umo,
            mid,
            emoji_id=int(emoji) if str(emoji).isdigit() else None,
            force=True,
        )
        if ok:
            self._react_join_at[key] = now
            logger.info(
                f"[human_sim] +1 跟贴表情（{umo.rsplit(':', 1)[-1]} 消息 {mid}）："
                f"{emoji} 来自 {reactor}"
            )

    async def _ack_reaction(self, umo: str, uid: str) -> None:
        """被贴了明确表情时，小概率软软回一句（不每次都回，避免刷屏）。"""
        lines = [
            "嘿嘿，收到你的表情啦~",
            "你这一个表情我懂了",
            "摸摸头，乖",
            "知道你在线啦",
        ]
        try:
            line = random.choice(lines)
            chain = []
            if str(uid).isdigit():
                chain.append(At(qq=int(uid)))
            chain.append(Plain(" " + line))
            await self._send(umo, chain)
            self._note_act(umo, line, spoken=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 表情回应文字回执失败: {type(e).__name__}: {e}")
