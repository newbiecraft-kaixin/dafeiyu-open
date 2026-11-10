"""黄脸池、face_config 读取与表情学习同步（从 media.py 按职责拆出）。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from astrbot.api import logger

from .config import (
    EXTRA_SYSFACE_PATH,
    FACE_CONFIG_CANDIDATES,
    FACE_IDS,
    FACE_REFRESH_SECONDS,
)

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"
    # _super_faces 的兜底路径按 main.py 所在目录计算，保持拆分前的取值。
    __file__ = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")


class MediaFacesMixin:

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
