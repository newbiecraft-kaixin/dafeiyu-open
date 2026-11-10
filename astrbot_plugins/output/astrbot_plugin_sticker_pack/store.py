"""收藏库路径、待确认索引、缺图需求、每日额度与入库/暂存。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid

from astrbot.api import logger


class StoreMixin:
    # ================= 收藏：图片查重入库 =================
    # 目录结构（与主库物理隔离，避免新图污染原有库）：
    #   data/stickers/fav/map_fav.txt    收藏库索引，格式同 map.txt
    #   data/stickers/fav/<名字>.gif     收藏的图
    #   data/stickers/fav/_pending/      已收下但还没问出含义的图
    #   data/stickers/fav/_hash_v3.json  全库图片指纹缓存（md5+dHash+aHash+直方图，查重用）
    def _fav_dir(self) -> str:
        return os.path.join(self.pack_dir, "fav")

    def _fav_map_path(self) -> str:
        return os.path.join(self._fav_dir(), "map_fav.txt")

    def _pending_dir(self) -> str:
        return os.path.join(self._fav_dir(), "_pending")

    def _pending_index(self) -> str:
        return os.path.join(self._fav_dir(), "_pending.json")

    def _hash_cache_path(self) -> str:
        return os.path.join(self._fav_dir(), "_hash_v3.json")

    def _load_pending(self) -> None:
        p = self._pending_index()
        if not os.path.exists(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return
        ttl = float(self.config.get("fav_pending_timeout_minutes", 10)) * 60
        now = time.time()
        keep = {}
        for umo, info in data.items():
            if not isinstance(info, dict):
                continue
            if now - float(info.get("ts", 0)) > ttl:
                try:
                    os.remove(info.get("file", ""))
                except OSError:
                    pass
                continue
            if os.path.exists(info.get("file", "")):
                keep[umo] = info
        self._pending = keep

    def _save_pending(self) -> None:
        try:
            os.makedirs(self._fav_dir(), exist_ok=True)
            with open(self._pending_index(), "w", encoding="utf-8") as f:
                json.dump(self._pending, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    def _note_deficit(self, word: str, umo: str) -> None:
        """匹配不到合适的图 = 库里缺这个情绪，记一笔，等群里出现新图时优先收。"""
        word = (word or "").strip()[:20]
        if not word:
            return
        ttl = float(self.config.get("deficit_ttl_minutes", 30)) * 60
        now = time.time()
        self._deficit = [d for d in self._deficit if now - d["ts"] < ttl]
        if any(d["word"] == word for d in self._deficit):
            return
        self._deficit.append({"word": word, "umo": umo, "ts": now})
        logger.info(f"[sticker_pack] 记录缺图需求：{word}（累计 {len(self._deficit)} 条）")

    def _deficit_hint(self) -> str:
        ttl = float(self.config.get("deficit_ttl_minutes", 30)) * 60
        now = time.time()
        self._deficit = [d for d in self._deficit if now - d["ts"] < ttl]
        return "、".join(d["word"] for d in self._deficit[-5:])

    # ---------- 自主收藏每日额度 ----------
    def _is_owner(self, uid) -> bool:
        """收藏表情是主人特权：别人的图一张都不收，也不占额度。"""
        return str(uid or "").strip() in self.admins

    def _quota_reset_if_new_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._collect_quota.get("date") != today:
            self._collect_quota = {"date": today, "count": 0}

    def _quota_left(self) -> int:
        """今天还能自主收几张；fav_daily_limit<=0 表示不限。"""
        limit = int(self.config.get("fav_daily_limit", 100) or 0)
        if limit <= 0:
            return 10 ** 9
        self._quota_reset_if_new_day()
        return max(limit - int(self._collect_quota.get("count", 0)), 0)

    def _take_quota(self) -> None:
        self._quota_reset_if_new_day()
        self._collect_quota["count"] = int(self._collect_quota.get("count", 0)) + 1
        self._save_rotate(force=True)

    def _fav_insert(self, pend: dict, name: str, meaning: str) -> tuple[bool, str]:
        """把待确认的图搬进收藏库：单独目录 + 单独索引，主库一个字节都不动。"""
        bad = self._banned_hint(name, meaning)
        if bad:
            try:
                os.remove(pend.get("file", ""))
            except Exception:  # noqa: BLE001
                pass
            return False, f"这名字/场合不太合适（「{bad}」），不收"
        base = re.sub(r"[^\w\u4e00-\u9fa5]", "", name)[:8] or "新收藏"
        name = base
        i = 2
        while name in self.stickers:
            name = f"{base}{i}"
            i += 1
        ext = os.path.splitext(pend.get("file", ""))[1] or ".png"
        dst = os.path.join(self._fav_dir(), f"{name}{ext}")
        try:
            os.makedirs(self._fav_dir(), exist_ok=True)
            if os.path.exists(pend.get("file", "")):
                shutil.move(pend["file"], dst)
            elif not os.path.exists(dst):
                # 源没了、目标也没有 = 真丢了；目标在 = 另一次调用已经搬好了
                return False, "入库失败：待确认的图不见了，重新发一次那张图吧"
            with open(self._fav_map_path(), "a", encoding="utf-8") as f:
                f.write(f"{name}:{meaning}:fav/{name}{ext}\n")
        except Exception as e:  # noqa: BLE001
            return False, f"入库失败：{type(e).__name__}: {e}"
        self.stickers[name] = {
            "meaning": meaning, "kind": "image", "value": dst, "source": "fav",
        }
        fp = pend.get("fp") or ({"d": pend["dhash"]} if pend.get("dhash") else None)
        if fp:
            self._dhash_map[name] = fp
            self._save_hash_cache()
        self._all_images_cache = []
        # 新收藏立刻进推荐索引，不用重启也不用等下一轮重建
        self._build_ranker()
        return True, name

    def _review_dir(self) -> str:
        """AI 自己收的图暂存这里：不进索引，网页里算「未入库」，核验后才入库。"""
        return os.path.join(self._fav_dir(), "_review")

    def _fav_park(self, pend: dict, name: str, meaning: str) -> tuple[bool, str]:
        """把 AI 自己收的图挪到待核验目录：只落盘，不写索引（网页里显示为未入库）。"""
        bad = self._banned_hint(name, meaning)
        if bad:
            try:
                os.remove(pend.get("file", ""))
            except Exception:  # noqa: BLE001
                pass
            return False, f"这名字/场合不太合适（「{bad}」），不收"
        ext = os.path.splitext(pend.get("file", ""))[1] or ".png"
        base = re.sub(r"[^\w\u4e00-\u9fa5]", "", name)[:8] or "待核验"
        dst = os.path.join(self._review_dir(), f"{base}_{uuid.uuid4().hex[:8]}{ext}")
        try:
            os.makedirs(self._review_dir(), exist_ok=True)
            if os.path.exists(pend.get("file", "")):
                shutil.move(pend["file"], dst)
            else:
                return False, "入库失败：待确认的图不见了，重新发一次那张图吧"
        except Exception as e:  # noqa: BLE001
            return False, f"入库失败：{type(e).__name__}: {e}"
        # 待核验的图也登记指纹：之前只落盘不登记，同一张图能反复被 AI 收进来，
        # 是收藏库大量重复的主因之一。
        fp = pend.get("fp") or ({"d": pend["dhash"]} if pend.get("dhash") else None)
        if not fp:
            fp = self._fingerprint(dst)
        if fp:
            self._dhash_map["@review:" + os.path.basename(dst)] = fp
            self._save_hash_cache()
        logger.info(f"[sticker_pack] 待核验「{name}」：{meaning} -> fav/_review/{os.path.basename(dst)}")
        return True, name
