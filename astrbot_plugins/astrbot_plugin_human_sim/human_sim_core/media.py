"""富媒体：表情/超级表情/表情包、黄脸池、学习同步与输出渲染。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
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

from .config import EXTRA_SYSFACE_PATH, FACE_CONFIG_CANDIDATES, FACE_IDS, FACE_REFRESH_SECONDS, LOCAL_DECIDE_PROVIDER
from .text import _MIX_TOKEN_RE, _NO_STICKER_REQ_RE, _STICKER_REQUEST_RE, _TOKEN_RE, _norm_request_text
from .runtime import _CLEANER

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"
    # _super_faces 的兜底路径按 main.py 所在目录计算，保持拆分前的取值。
    __file__ = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")


class MediaMixin:

    async def _send_super_series(self, umo: str, picked: list[str]) -> list[str]:
        """一条一条把超级表情发出去：真人不会把三个塞进同一条消息，连排也糊成一片。"""
        faces = self._super_faces()
        pairs = [(n, Face(id=int(faces[n]["id"]))) for n in picked if n in faces]
        await asyncio.sleep(random.uniform(0.6, 1.6))
        sent: list[str] = []
        for i, (name, comp) in enumerate(pairs):
            ok = await self._send(umo, [comp])
            if not ok:
                logger.warning(f"[human_sim] 超级表情第 {i + 1} 条没发出去")
                break
            sent.append(name)
            if i < len(pairs) - 1:
                await asyncio.sleep(random.uniform(0.8, 1.6))
        return sent

    def _emoji_pick_candidates(self, text: str, hist: list[str]) -> list[tuple[str, str]]:
        """给模型看的候选：先留句子里点到的、聊天里刚提到的，不够再随机补。"""
        faces = self._super_faces()
        comics = [n for _, n, _ in self._sticker_meta if len(n) >= 2]
        cap = max(4, int(self.cfg.get("emoji_ai_pick_candidates", 12)))
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for n in faces:
            if n and (n in text or any(n in h for h in hist)) and n not in seen:
                seen.add(n)
                out.append(("超级", n))
        for n in comics:
            if (n in text or any(n in h for h in hist)) and n not in seen:
                seen.add(n)
                out.append(("梗图", n))
        if len(out) < cap:
            pool = [("超级", n) for n in faces] + [("梗图", n) for n in comics]
            random.shuffle(pool)
            for k, n in pool:
                if len(out) >= cap:
                    break
                if n not in seen:
                    seen.add(n)
                    out.append((k, n))
        return out[:cap]

    async def _ai_pick_emoji(self, umo: str, text: str) -> tuple[str, str] | None:
        """让模型按聊天场景挑一个表情：返回 ("super"/"comic", 名字)，挑不出返回 None。"""
        faces = self._super_faces()
        comics = [n for _, n, _ in self._sticker_meta if len(n) >= 2]
        if not faces and not comics:
            return None
        hist = list(self._history.get(umo, []))[
            -max(1, int(self.cfg.get("emoji_ai_pick_history", 6))):
        ]
        ctx = "\n".join(hist) if hist else "（没有聊天记录）"
        cand = self._emoji_pick_candidates(text, hist)
        if not cand:
            return None
        meaning_map = {n: m for _, n, m in self._sticker_meta}
        menu = "\n".join(
            f"{k}:{n}"
            + (
                f"（{meaning_map[n]}）"
                if k == "梗图"
                and meaning_map.get(n)
                and meaning_map[n] != n
                else ""
            )
            for k, n in cand
        )
        prompt = (
            f"最近群里在聊：\n{ctx}\n\n"
            f"有人刚说：{text}\n\n"
            f"可发的表情：\n{menu}\n\n"
            "从上面挑一个最贴合此刻气氛的，直接输出它（照抄「超级:名字」或「梗图:名字」）。\n"
            "都不合适就输出「不发」。只输出一行，不要解释。"
        )
        provider = None
        if umo:
            try:
                provider = self.context.get_using_provider(umo)
            except Exception:  # noqa: BLE001
                provider = None
        provider = (
            provider
            or self.context.get_provider_by_id(LOCAL_DECIDE_PROVIDER)
            or self.context.get_using_provider()
        )
        if not provider:
            return None
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=uuid.uuid4().hex,
                    contexts=[],
                    system_prompt="你只负责从候选里挑一个表情，只输出一行结果。",
                    persist=False,
                ),
                timeout=float(self.cfg.get("emoji_ai_pick_timeout", 12)),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 表情 AI 挑选失败: {type(e).__name__}: {e}")
            return None
        raw = (resp.completion_text or "").strip()
        logger.info(f"[human_sim] 表情点单 AI 挑选结果: {raw[:40]!r}")
        if not raw or "不发" in raw:
            return None
        comic_set = set(comics)
        m = re.search(r"(超级|梗图)\s*[:：]\s*([^\s，,。;；、\n]+)", raw)
        kind, name = (None, "")
        if m:
            kind = "super" if m.group(1) == "超级" else "comic"
            name = m.group(2)
        else:
            name = re.sub(r"[「」『』“”\"'。，,、\n]", "", raw).strip()
        if not name:
            return None
        if kind == "super" and name in faces:
            return ("super", name)
        if kind == "comic" and name in comic_set:
            return ("comic", name)
        if name in faces:
            return ("super", name)
        if name in comic_set:
            return ("comic", name)
        logger.info(f"[human_sim] 表情 AI 挑选无匹配结果: {raw[:40]!r}")
        return None

    async def _handle_emoji_request(self, event: AstrMessageEvent) -> bool:
        """「发个表情 / 发个打龟 / 发个超级赞」：发什么交给模型按场景挑。

        以前这里按固定优先级抢发（超级表情 > 梗图），同一句话有几种含义时规则永远选同一种。
        现在先让模型看最近聊天和候选挑一个，挑不出来才回落到两个硬规则处理函数。
        """
        if not self.cfg.get("emoji_ai_pick", True):
            return False
        text = _norm_request_text(event.message_str or "")
        if not text or len(text) > 20:
            return False
        if _NO_STICKER_REQ_RE.search(text):
            return False
        if not re.search(r"发|来|整|给|甩|要", text):
            return False
        faces = self._super_faces()
        comics = [n for _, n, _ in self._sticker_meta if len(n) >= 2]
        if not (
            _STICKER_REQUEST_RE.search(text)
            or re.search(r"超级(?:大)?表情|大表情", text)
            or any(n and n in text for n in faces)
            or any(n in text for n in comics)
        ):
            return False
        umo = event.unified_msg_origin
        now = datetime.now().timestamp()
        uid = str(event.get_sender_id() or "")
        key = f"last_emoji_pick:{umo}:{uid}"
        if now - float(self.state.get(key, 0) or 0) < 15:
            return False
        pick = await self._ai_pick_emoji(umo, text)
        if not pick:
            return False
        kind, name = pick
        if kind == "super":
            count = 1
            if re.search(r"几个|几张|一些|多点|多来|再来", text):
                count = 3
            elif re.search(r"两|2", text):
                count = 2
            picked = [name]
            rest = [n for n in faces if n != name]
            if len(picked) < count and rest:
                picked += random.sample(rest, min(count - 1, len(rest)))
            sent = await self._send_super_series(umo, picked)
            if not sent:
                return False
            self._note_act(umo, f"发了超级表情：{'、'.join(sent)}")
            logger.info(
                f"[human_sim] 点单 AI 按场景选超级表情「{name}」→ {'、'.join(sent)}"
            )
        else:
            path = next((p for p, n, _ in self._sticker_meta if n == name), None)
            if not path:
                return False
            await asyncio.sleep(random.uniform(0.6, 1.6))
            try:
                await self._send(umo, [Image.fromFileSystem(path)])
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[human_sim] 点单 AI 选的梗图发送失败: {type(e).__name__}: {e}"
                )
                return False
            self._note_act(umo, f"发了表情包：{self._sticker_desc(path)}")
            logger.info(
                f"[human_sim] 点单 AI 按场景选梗图「{name}」→ {os.path.basename(path)}"
            )
        self.state[key] = now
        return True

    async def _handle_sticker_request(self, event: AstrMessageEvent) -> bool:
        """「发个表情 / 来张图 / 斗图」这种点单：本地直接甩图，不等 AI。

        为什么必须本地做：本地 9B 模型基本不会主动调 send_meme，
        靠它自己发就等于不发（日志里这两个工具一整天都是 0 次）。用户张嘴要了，
        这里就必须真发一张出去。
        """
        if not self.cfg.get("sticker_request_local", True):
            return False
        text = (event.message_str or "").strip()
        # 只接短句点单，避免把「这个表情包好好笑」之类的闲聊也当成点单
        # 先剥 [At:123]/@昵称 占位，否则 @ 一下就把 12 字门槛撑爆
        text = _norm_request_text(text)
        if not text or len(text) > 12:
            return False
        # 只是提到「表情包」、或者「不要/不用」这类否定句，都不是点单
        if _NO_STICKER_REQ_RE.search(text):
            return False
        # 两条入口：
        # ①「发个表情/斗图/来张图」这种泛点单
        # ②「发个打龟/来个打龟」这种点名点单 —— 名字得在 map.txt 里（打龟、干饭、抱抱…）
        named_paths = [
            p for p, name, _ in self._sticker_meta if len(name) >= 2 and name in text
        ]
        named_order = bool(named_paths) and bool(re.search(r"发|来|整|给|甩|要", text))
        if not _STICKER_REQUEST_RE.search(text) and not named_order:
            return False
        # 不卡 watch_groups：只要有人张嘴要，哪个群都得真发出去
        umo = event.unified_msg_origin
        if not self._stickers:
            return False
        now = datetime.now().timestamp()
        # 同一个人 15 秒内只发一张，防连点「再来一张」刷屏；换个人要就照发
        uid = str(event.get_sender_id() or "")
        key = f"last_sticker_req:{umo}:{uid}"
        if now - float(self.state.get(key, 0) or 0) < 15:
            return False
        path = self._match_sticker(text) or random.choice(self._stickers)
        await asyncio.sleep(random.uniform(0.6, 1.6))
        try:
            await self._send(umo, [Image.fromFileSystem(path)])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 点单表情发送失败: {type(e).__name__}: {e}")
            return False
        self.state[key] = now
        self._note_act(umo, f"发了表情包：{self._sticker_desc(path)}")
        logger.info(
            f"[human_sim] 点单表情「{text}」本地发送：{os.path.basename(path)}"
        )
        return True

    def _read_face_config(self) -> list[dict]:
        """读 QQ 官方 face_config.json 里的 sysface 行；读不到返回空"""
        for p in FACE_CONFIG_CANDIDATES:
            if not p or not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                continue
            rows = cfg.get("sysface") if isinstance(cfg, dict) else None
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        return []

    def _face_pool(self) -> list[int]:
        """官方黄脸 id 池：从 face_config.json 动态取，取不到才用旧常量"""
        if self._face_pool_cache and time.time() - self._face_cfg_ts < FACE_REFRESH_SECONDS:
            return self._face_pool_cache
        ids: list[int] = []
        for r in self._read_face_config():
            if r.get("AniStickerType"):
                continue
            sid = str(r.get("QSid") or "").strip()
            if sid.isdigit():
                ids.append(int(sid))
        self._face_pool_cache = ids or list(FACE_IDS)
        self._face_cfg_ts = time.time()
        return self._face_pool_cache

    def _super_faces(self) -> dict:
        """QQ 超级表情名 → face id：直接读官方 face_config.json，不再依赖静态 json。"""
        if (
            self._super_faces_cache is not None
            and time.time() - self._face_cfg_ts < FACE_REFRESH_SECONDS
        ):
            return self._super_faces_cache
        fresh: dict[str, dict] = {}
        for r in self._read_face_config():
            if not r.get("AniStickerType"):
                continue
            sid = str(r.get("QSid") or "").strip()
            name = str(r.get("QDes") or "").lstrip("/").strip()
            if sid and name:
                fresh[name] = {
                    "id": sid,
                    "type": int(r.get("AniStickerType") or 0),
                    "pack": str(r.get("AniStickerPackName") or ""),
                }
        if fresh:
            # 合并从群聊里学到的表情（官方 face_config 没跟上的新包，靠真实消息补）
            for name, info in self._learned_faces().items():
                if name not in fresh and info.get("id"):
                    fresh[name] = {
                        "id": str(info["id"]),
                        "type": 1,
                        "pack": "群聊学到",
                    }
            self._super_faces_cache = fresh
            self._face_cfg_ts = time.time()
            return fresh
        # 官方配置读不到（换机器等）才退回 basic_tools 的旧缓存
        try:
            here = os.path.abspath(__file__)
            p = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(here))),
                "plugin_data", "astrbot_plugin_basic_tools", "super_faces.json",
            )
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._super_faces_cache = data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 读取超级表情表失败: {type(e).__name__}: {e}")
            self._super_faces_cache = {}
        self._face_cfg_ts = time.time()
        return self._super_faces_cache

    # ---------- 从群聊消息自动学习表情（face_config 跟不上 QQ 新包时兜底） ----------

    def _learned_faces(self) -> dict:
        try:
            d = self.state.get("learned_faces")
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _learn_face(self, fid: object, name: object, raw: dict | None = None) -> bool:
        """记一条 群里真实出现过的 face id → 名字（hashmap 直查）。返回是否是新学的。

        老面孔只刷新 last_seen（1 小时内不重复写盘）；超过 7 天没出现过的
        由 _prune_learned_faces 定期删掉，表不会越长越大。
        raw=NapCat face 段的 data.raw，里面的 packId/stickerId 用来让 NapCat 发得出去。
        """
        fid = str(fid or "").strip()
        name = re.sub(r"^[\[\[【/／]+|[\]】\]]+$", "", str(name or "").strip()).strip()
        if not fid.isdigit() or not name or len(name) > 20:
            return False
        learned = self._learned_faces()
        cur = learned.get(name)
        meta = {}
        if isinstance(raw, dict):
            if str(raw.get("packId") or "").strip():
                meta["pack"] = str(raw.get("packId")).strip()
            if str(raw.get("stickerId") or "").strip():
                meta["sticker"] = str(raw.get("stickerId")).strip()
            if str(raw.get("stickerType") or "").strip():
                meta["stype"] = str(raw.get("stickerType")).strip()
        if cur and str(cur.get("id")) == fid:
            changed = False
            for k, v in meta.items():
                if cur.get(k) != v:      # 老条目只有 id，补上 pack/sticker 就能发了
                    cur[k] = v
                    changed = True
            if changed or time.time() - float(cur.get("ts") or 0) > 3600:
                cur["ts"] = time.time()
                self.state["learned_faces"] = learned
                self._save_state()
                return changed
            return False
        entry = {"id": fid, "ts": time.time()}
        entry.update(meta)
        learned[name] = entry
        self.state["learned_faces"] = learned
        return True

    def _sync_extra_sysface(self) -> None:
        """把带 pack/sticker 元数据的学习表情写进 NapCat 外部表情表。

        napcat.mjs 的 face 发送会先查内置表，查不到再读这个文件；
        所以写进去之后，新表情立刻能发，不用改代码也不用重启 NapCat。
        """
        entries = []
        for name, info in self._learned_faces().items():
            fid = str(info.get("id") or "")
            if not fid.isdigit() or not str(info.get("pack") or "").strip():
                continue
            entries.append({
                "QSid": fid,
                "QDes": f"/{name}",
                "IQLid": fid,
                "AQLid": fid,
                "EMCode": "10" + fid,
                "AniStickerType": int(str(info.get("stype") or 1) or 1),
                "AniStickerPackId": str(info["pack"]),
                "AniStickerId": str(info.get("sticker") or ""),
            })
        if not entries:
            return
        old = []
        try:
            with open(EXTRA_SYSFACE_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            old = old if isinstance(old, list) else []
        except (OSError, ValueError):
            old = []
        merged: dict[str, dict] = {}
        for e in old:
            if isinstance(e, dict) and str(e.get("QSid") or "").isdigit():
                merged[str(e["QSid"])] = e
        # 合并而不是覆盖：学习到的条目可能只有 pack 没有 sticker，
        # 直接替换会把旧表里完整的 stickerId 抹掉，那表情就又发不出来了
        for e in entries:
            cur = merged.setdefault(e["QSid"], {"QSid": e["QSid"]})
            for k, v in e.items():
                if k == "QSid" or v not in ("", None, 0):
                    cur[k] = v
        try:
            tmp = EXTRA_SYSFACE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(merged.values()), f, ensure_ascii=False, indent=1)
            os.replace(tmp, EXTRA_SYSFACE_PATH)
        except OSError as e:
            logger.debug(f"[human_sim] 写外部表情表失败: {type(e).__name__}: {e}")

    async def _sync_faces_from_history(self) -> int:
        """定时从群历史里补学超级表情，不用等它在群里重新出现一次。

        群历史里的 face 段带完整 raw（faceType/packId/stickerId），学完写进
        NapCat 外部表情表；napcat.mjs 按文件 mtime 热读，全程不需要重启。
        每 6 小时最多跑一次，避免频繁拉大包历史。
        """
        now = time.time()
        if now - float(self.state.get("faces_history_ts", 0) or 0) < 6 * 3600:
            return 0
        bot = next(iter(self._bots.values()), None)
        if bot is None:
            return 0
        self.state["faces_history_ts"] = now
        n = 0
        scanned = 0
        for umo in self.cfg["watch_groups"]:
            gid = str(umo).rsplit(":", 1)[-1]
            if not gid.isdigit():
                continue
            try:
                res = await asyncio.wait_for(
                    bot.api.call_action(
                        "get_group_msg_history", group_id=int(gid), count=100
                    ),
                    timeout=20,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[human_sim] 拉群历史失败 {gid}: {type(e).__name__}: {e}")
                continue
            msgs = ((res.get("data") if isinstance(res, dict) else {}) or {}).get("messages") or []
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                for s in (m.get("message") or []):
                    if not isinstance(s, dict) or s.get("type") != "face":
                        continue
                    d = s.get("data") or {}
                    raw = d.get("raw") if isinstance(d.get("raw"), dict) else {}
                    if str(raw.get("faceType") or "") != "3":
                        continue
                    scanned += 1
                    if self._learn_face(d.get("id"), raw.get("faceText"), raw):
                        n += 1
        logger.info(f"[human_sim] 群历史表情扫描：{scanned} 条超级表情，新学 {n} 个")
        if n:
            self._save_state()
            self._sync_extra_sysface()
            self._super_faces_cache = None
            self._face_cfg_ts = 0.0
            logger.info(f"[human_sim] 群历史补学超级表情 {n} 个（已写入外部表，无需重启）")
        return n

    def _prune_learned_faces(self) -> int:
        """删掉 7 天没在群里出现过的学到的表情（官方 face_config 的不受影响）。

        老条目没有 ts 字段的，视为刚见过（补上时间戳），不误删。
        """
        ttl = 7 * 86400
        learned = self._learned_faces()
        now = time.time()
        dead = []
        for n, v in learned.items():
            ts = float(v.get("ts") or 0)
            if ts <= 0:
                v["ts"] = now
            elif now - ts > ttl:
                dead.append(n)
        for n in dead:
            del learned[n]
        if dead:
            self.state["learned_faces"] = learned
            self._save_state()
            self._super_faces_cache = None
            self._face_cfg_ts = 0.0
        return len(dead)

    def _learn_from_event(self, event) -> int:
        """输入层顺带学表情（NapCat 的 data.text 就是表情名），不走 AI。"""
        n = 0
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            segs = raw.get("message") if isinstance(raw, dict) else None
            if isinstance(segs, list):
                for s in segs:
                    if isinstance(s, dict) and s.get("type") == "face":
                        d = s.get("data") or {}
                        # NapCat 的 face 段常常没有 data.text，名字在 data.raw.faceText
                        # （超级表情尤其如此，如 524 = /朵莉亚），不读这里就永远学不到
                        raw_d = d.get("raw") if isinstance(d.get("raw"), dict) else {}
                        # 只学超级表情（faceType 3）；普通黄脸本来就在 face_config 里，
                        # 混进来会把「随机发个超级表情」抽成小黄脸
                        if str(raw_d.get("faceType") or "") != "3":
                            continue
                        nm = (
                            d.get("text")
                            or raw_d.get("faceText")
                            or d.get("summary")
                            or ""
                        )
                        if self._learn_face(d.get("id"), nm, raw_d):
                            n += 1
        except Exception:  # noqa: BLE001
            return 0
        # 过期清理跟着输入层走：每 6 小时最多跑一次，dict 遍历毫秒级
        now = time.time()
        if now - float(self.state.get("learned_faces_ts", 0) or 0) > 6 * 3600:
            self.state["learned_faces_ts"] = now
            self._prune_learned_faces()
            self._save_state()  # 清理时间戳落盘
        if n:
            self._save_state()
            self._sync_extra_sysface()
            self._super_faces_cache = None
            self._face_cfg_ts = 0.0
        return n

    async def _handle_super_face_request(self, event: AstrMessageEvent) -> bool:
        """「发个超级表情 / 来个超级赞」：本地直接发，不等 AI。

        超级表情（face id 360/362/364 这类）模型经常嘴上答应了、实际不调
        send_super_face，用户看到的就是「说了发却没发」。这里本地兜住。
        """
        if not self.cfg.get("super_face_request_local", True):
            return False
        raw_text = (event.message_str or "").strip()
        text = _norm_request_text(raw_text)
        if not text or len(text) > 30:
            if "表情" in raw_text:
                logger.info(
                    f"[human_sim] 点单表情未识别（长度）：raw={raw_text[:40]!r} clean={text!r}"
                )
            return False
        faces = self._super_faces()
        if not faces:
            return False
        named = [n for n in faces if n and n in text]
        if not named and not re.search(r"超级(?:大)?表情|大表情", text):
            return False
        # 必须是「要」的动作，避免把「超级表情好好玩」这种闲聊当点单
        if not re.search(r"发|来|整|给|甩|要", text):
            return False
        umo = event.unified_msg_origin
        now = datetime.now().timestamp()
        uid = str(event.get_sender_id() or "")
        key = f"last_super_req:{umo}:{uid}"
        if now - float(self.state.get(key, 0) or 0) < 15:
            return False
        count = 1
        if re.search(r"几个|几张|一些|多点|多来|再来", text):
            count = 3
        elif re.search(r"两|2", text):
            count = 2
        if named:
            picked = named[:count]
            rest = [n for n in faces if n not in picked]
            if len(picked) < count and rest:
                picked += random.sample(rest, min(count - len(picked), len(rest)))
        else:
            picked = random.sample(list(faces), min(count, len(faces)))
        sent = await self._send_super_series(umo, picked)
        if not sent:
            return False
        self.state[key] = now
        self._note_act(umo, f"发了超级表情：{'、'.join(sent)}")
        logger.info(
            f"[human_sim] 点单超级表情「{text}」本地分 {len(sent)} 条发送：{'、'.join(sent)}"
        )
        return True

    def _norm_fw_token(self, m: re.Match, name_to_path: dict) -> str:
        """全角占位符单个归一：库里有 → 半角；纯英文名一定不是正文 → 抹掉；中文保留。"""
        name = m.group(1)
        if name in name_to_path or name in self._face_by_name:
            return f":{name}:"
        return "" if name.isascii() else m.group(0)

    def _render_placeholders(self, chain: list) -> list:
        """输出层兜底：把 :名称: 换成对应表情图；库里没有的名字直接抹掉。

        主动发言（插话/冷场/拍一拍应答…）不经过 sticker_pack 的
        on_decorating_result，裸占位符会原样发到群里，所以在这里统一收口。
        一条消息最多塞 max_stickers_per_message 张图（默认 1），已有的图也算数。
        """
        if not any(
            isinstance(c, Plain) and _MIX_TOKEN_RE.search(c.text or "") for c in chain
        ):
            return chain
        name_to_path: dict[str, str] = {}
        for path, name, _ in self._sticker_meta:
            name_to_path.setdefault(name, path)
        # 冒号写法五花八门（:吃瓜:、：吃瓜：、:吃瓜：），先统一归一成半角，
        # 库里有这个名字才换；不认识的中文保留原文，纯英文/数字一定不是正文，抹掉
        for c in chain:
            if isinstance(c, Plain) and c.text and (":" in c.text or "：" in c.text):
                c.text = _MIX_TOKEN_RE.sub(
                    lambda m: self._norm_fw_token(m, name_to_path), c.text
                )
        media = sum(1 for c in chain if isinstance(c, (Image, Face)))
        budget = max(0, int(self.cfg.get("max_stickers_per_message", 1)) - media)
        out: list = []
        for comp in chain:
            if not isinstance(comp, Plain):
                out.append(comp)
                continue
            text = comp.text or ""
            pos = 0
            for m in _TOKEN_RE.finditer(text):
                before = text[pos:m.start()]
                if before.strip():
                    out.append(Plain(before))
                token = m.group(1)
                path = name_to_path.get(token)
                fid = self._face_by_name.get(token)
                if path and budget > 0:
                    out.append(Image.fromFileSystem(path))
                    budget -= 1
                elif fid and budget > 0:
                    out.append(Face(id=fid))
                    budget -= 1
                elif path or fid:
                    logger.info(
                        f"[human_sim] 占位符 :{token}: 已到单条图片上限，转为不发"
                    )
                else:
                    # 清洗层的名字表和这里不完全一致（它还认 face_map.json），
                    # 认不出就按语义再找一张，别让模型挑的表情凭空消失
                    fb = self._match_sticker(token) if budget > 0 else None
                    if fb:
                        out.append(Image.fromFileSystem(fb))
                        budget -= 1
                        logger.info(f"[human_sim] 表情名 :{token}: 未收录，按语义挑了 {os.path.basename(fb)}")
                    else:
                        logger.info(f"[human_sim] 丢弃库外占位符 :{token}:")
                pos = m.end()
            tail = text[pos:]
            if tail.strip():
                out.append(Plain(tail))
        if not any(isinstance(c, Plain) and (c.text or "").strip() for c in out) and not any(
            isinstance(c, (Image, Face)) for c in out
        ):
            return []
        return out

    def _humanize(self, text: str) -> str:
        """主动插话的文本不过 on_decorating_result，必须自己洗一遍再发。"""
        if not text:
            return ""
        # 占位符不在这里抹：留给 _send 的输出层统一换成真表情/图，抹掉就少了表情
        cleaned = _CLEANER.humanize(text)[0] if _CLEANER else text.strip()
        cleaned = cleaned.strip()
        # 洗成「……」说明全是套话，这种宁可不发（主动搭话不能没内容）
        if not cleaned.strip("…。， "):
            logger.info("[human_sim] 清洗后只剩沉默词，放弃这条主动消息")
            return ""
        return cleaned

    def _sticker_desc(self, path: str) -> str:
        """把表情文件写成人能看懂的描述：名称（意思）。

        以前历史里只记文件名（n296.gif）或不带意思的名称，模型下一轮
        根本不知道自己刚发的那张图是什么意思。
        """
        p = str(path or "")
        base = os.path.basename(p)
        for sp, name, meaning in self._sticker_meta:
            if sp == p or os.path.basename(sp) == base:
                if meaning and meaning != name:
                    return f"{name}（{meaning}）"
                return name
        return base

    def _face_desc(self, fid) -> str:
        """黄脸 id 反查名字，写回上下文时别只留一个数字。"""
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            return "表情"
        for name, i in self._face_by_name.items():
            if int(i) == fid:
                return name
        return f"表情{fid}"

    def _describe_chain_media(self, chain: list) -> list[str]:
        """把一条消息里的图片/黄脸转成能写进上下文的描述。"""
        out: list[str] = []
        for comp in chain or []:
            if isinstance(comp, Image):
                path = getattr(comp, "path", "") or getattr(comp, "file", "") or ""
                out.append(self._sticker_desc(path))
            elif isinstance(comp, Face):
                out.append(self._face_desc(getattr(comp, "id", None)))
        return out

    def _describe_tokens(self, text: str) -> list[str]:
        """回复里写的 :名称: 实际会被换成表情，取出名称和意思。"""
        if not text:
            return []
        known = {n for _, n, _ in self._sticker_meta}
        meaning = {n: m for _, n, m in self._sticker_meta}
        out: list[str] = []
        for m in _MIX_TOKEN_RE.finditer(text):
            token = m.group(1)
            if token in self._face_by_name:
                out.append(token)
            elif token in known:
                mn = meaning.get(token) or ""
                out.append(f"{token}（{mn}）" if mn and mn != token else token)
        return out

    def _note_tokens(self, umo: str, text: str) -> None:
        """回复里的 :名称: 发送时会被换掉，历史里补一条带意思的说明。"""
        descs = self._describe_tokens(text)
        if descs and umo:
            self._note_act(umo, f"发了表情包：{'、'.join(descs[:2])}")

    def _match_sticker(self, text: str) -> str | None:
        """按消息内容智能挑梗图：先匹配名字/意思关键词，再按字符重叠度，都不行随机"""
        if not self._sticker_meta:
            return None
        text = text or ""
        # 1) 消息里正好提到某个表情名 → 用它
        for path, name, _ in self._sticker_meta:
            if len(name) >= 2 and name in text:
                return path
        # 2) 字符重叠度门槛提到 3，防「聊鱼蹦鱼片」式误配；配不上就纯随机
        chars = set(text)
        best, best_score = None, 2
        for path, name, meaning in self._sticker_meta:
            score = len((set(name) | set(meaning)) & chars)
            if score > best_score:
                best, best_score = path, score
        return best or random.choice(self._stickers)

    def _maybe_append_emoji(
        self, chain: list, text: str = "", force: bool = False, umo: str = ""
    ) -> None:
        # 长文（故事/干货）后面甩个表情很怪，直接不加；对方明着要表情则无视长度
        if not force and len(text or "") > 120:
            return
        r = random.random()
        note = ""
        if self._stickers and (force or r < float(self.cfg["sticker_probability"])):
            path = self._match_sticker(text)
            if path:
                chain.append(Image.fromFileSystem(path))
                note = f"回复里带了个表情包：{self._sticker_desc(path)}"
                if force:
                    logger.info(f"[human_sim] 按要求补发表情: {os.path.basename(path)}")
        elif not force and r < float(self.cfg["sticker_probability"]) + float(
            self.cfg["face_probability"]
        ):
            pool = self._map_faces or self._face_pool()
            fid = random.choice(pool)
            chain.append(Face(id=fid))
            note = f"回复里带了个表情：{self._face_desc(fid)}"
        if note and umo:
            self._note_act(umo, note)

    async def _maybe_sticker_back(self, event: AstrMessageEvent, umo: str) -> bool:
        """对方只甩了个表情包：不写字，回一个表情（斗图）。

        主人或被点名的必回；群友按概率回。同群冷却内不重复，省得两个人对着刷图。
        """
        try:
            cd = float(self.cfg.get("sticker_back_cooldown_seconds", 240) or 240)
        except (TypeError, ValueError):
            cd = 240.0
        now = datetime.now().timestamp()
        if now - float(self.state.get(f"last_sticker_back:{umo}", 0) or 0) < cd:
            return False
        uid = str(event.get_sender_id() or "")
        owner = str(self.cfg.get("owner_id") or "")
        directed = uid == owner or any(
            n and n in (event.message_str or "") for n in (self.cfg.get("bot_names") or [])
        )
        try:
            p = 1.0 if directed else float(self.cfg.get("sticker_back_probability", 0.35))
        except (TypeError, ValueError):
            p = 0.35
        if random.random() > p:
            return False
        if not self._global_gate(umo):
            return False
        if self._stickers and random.random() < 0.7:
            path = random.choice(self._stickers)
            chain: list = [Image.fromFileSystem(path)]
            desc = self._sticker_desc(path)
        else:
            fid = random.choice(self._map_faces or self._face_pool())
            chain = [Face(id=fid)]
            desc = self._face_desc(fid)
        await asyncio.sleep(random.uniform(0.6, 1.6))
        try:
            await self._send(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 斗图回表情失败: {type(e).__name__}: {e}")
            return False
        self.state[f"last_sticker_back:{umo}"] = now
        self._note_act(
            umo,
            f"回了{'个表情包' if isinstance(chain[0], Image) else '个表情'}：{desc}",
        )
        logger.info(
            f"[human_sim] 斗图回了{'个表情包' if isinstance(chain[0], Image) else '个黄脸'}"
            f"（{umo.rsplit(':', 1)[-1]}，{'主人/点名' if directed else '群友'}）"
        )
        return True
