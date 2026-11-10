"""轮换状态、出站闸/密度闸与后台巡检循环。"""

from __future__ import annotations

import asyncio
import json
import os
import time

from astrbot.api import logger


class StateMixin:
    def _fstat(self, key: str) -> None:
        try:
            self._follow_stats[key] = self._follow_stats.get(key, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    async def _monitor_loop(self) -> None:
        """后台限额度量：每 5 分钟上报补表情流程的通过/拦截/发送，判断限额是否合理"""
        while True:
            await asyncio.sleep(300)
            try:
                s = self._follow_stats
                self._follow_stats = dict.fromkeys(s, 0)
                if any(s.values()):
                    logger.debug(
                        "[sticker_pack] 近5分钟补表情度量 | "
                        f"触发通过 {s['pass']}，发送 {s['sent']}，"
                        f"拦：概率 {s['block_prob']} / 分钟上限 {s['block_cap']} / "
                        f"互斥 {s['block_running']} / 冷却 {s['block_cool']}，"
                        f"直发 {s.get('direct', 0)}，模型兜底 {s.get('llm_fallback', 0)}，"
                        f"AI 弃选 {s['no_pick']}，空池 {s['pool_empty']}，"
                        f"发送失败 {s['send_fail']}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[sticker_pack] 监测上报失败: {type(e).__name__}: {e}")

    async def _maintenance_loop(self) -> None:
        """动态维护：周期重算指纹（网页/手动往库外丢图也能跟上）+ 清理过期文件。"""
        interval = max(5, int(self.config.get("hash_rescan_minutes", 30) or 30)) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._refresh_hashes()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[sticker_pack] 指纹巡检失败: {type(e).__name__}: {e}")
            try:
                self._cleanup_stale_files()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[sticker_pack] 清理巡检失败: {type(e).__name__}: {e}")

    # ---------- 表情选择：名字直配 / 情绪词随机抽 / 最近使用冷却 ----------
    def _recent_used(self, umo: str) -> set[str]:
        return set(self._recent.get(umo, []))

    def _mark_used(self, umo: str, name: str) -> None:
        window = int(self.config.get("recent_window", 30))
        lst = self._recent.setdefault(umo, [])
        lst.append(name)
        del lst[:-max(window, 1)]
        cnt = self._use_count.setdefault(umo, {})
        cnt[name] = cnt.get(name, 0) + 1
        self._save_rotate()

    # ---------- 轮换状态落盘（重启后不遗忘「刚发过谁 / 发过几次」）----------
    def _load_rotate(self) -> None:
        try:
            with open(self._rotate_path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError, TypeError):
            return
        try:
            window = max(int(self.config.get("recent_window", 30)), 1)
            recent = data.get("recent") or {}
            face_recent = data.get("face_recent") or {}
            use_count = data.get("use_count") or {}
            if isinstance(recent, dict):
                self._recent = {
                    str(k): [str(x) for x in v][-window:]
                    for k, v in recent.items() if isinstance(v, list) and v
                }
            if isinstance(face_recent, dict):
                self._face_recent = {
                    str(k): [str(x) for x in v][-4:]
                    for k, v in face_recent.items() if isinstance(v, list) and v
                }
            if isinstance(use_count, dict):
                # 每个群只留用得最多的 80 张，避免文件无限长
                self._use_count = {}
                for k, v in use_count.items():
                    if not isinstance(v, dict):
                        continue
                    ranked = sorted(
                        ((str(n), int(c)) for n, c in v.items() if int(c) > 0),
                        key=lambda x: -x[1],
                    )[:80]
                    self._use_count[str(k)] = dict(ranked)
            quota = data.get("collect_quota") or {}
            if isinstance(quota, dict):
                self._collect_quota = {
                    "date": str(quota.get("date") or ""),
                    "count": max(int(quota.get("count") or 0), 0),
                }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 轮换状态读取异常（忽略）: {e}")

    def _save_rotate(self, force: bool = False) -> None:
        """节流写盘：表情发得不快，30 秒内最多写一次就够，但退出前可 force。"""
        self._rotate_dirty = True
        now = time.time()
        if not force and now - self._rotate_saved_at < 30:
            return
        try:
            payload = {
                "recent": self._recent,
                "face_recent": self._face_recent,
                "use_count": self._use_count,
                "collect_quota": self._collect_quota,
            }
            tmp = self._rotate_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._rotate_path)
            self._rotate_saved_at = now
            self._rotate_dirty = False
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[sticker_pack] 轮换状态写盘失败: {e}")

    def _all_images(self) -> list[str]:
        if not self._all_images_cache:
            self._all_images_cache = [
                n for n, v in self.stickers.items() if v.get("kind") == "image"
            ]
        return self._all_images_cache

    # ---------- 出站闸：所有表情路径共用 ----------
    # 单发 / 语义配脸 / 补表情 三条路原来各管各的，一条长回复被拆成三条消息时
    # 每条都能各配一张，看起来就是「连发一大堆单张」。这里统一记同群上一次出站时间。
    def _emit_allowed(self, umo: str) -> bool:
        gap = float(self.config.get("sticker_min_gap_seconds", 120))
        if gap > 0 and time.time() - float(self._last_emit.get(umo, 0.0)) < gap:
            return False
        # 密度闸：同群最近 window 内，平均每 ratio 条文字才配得上 1 张表情。
        # 不看这一条说的是什么，只看「最近发了多少条、配了几张」，所以群里刷得
        # 再快也不会变成一条文字一条图。
        ratio = int(self.config.get("sticker_text_per_sticker", 5))
        if ratio <= 0:
            return True
        window = float(self.config.get("sticker_density_window_seconds", 600))
        cutoff = time.time() - window
        ev = [e for e in self._density.get(umo, []) if e[0] >= cutoff]
        texts = sum(1 for _ts, kind in ev if kind == "text")
        emojis = sum(1 for _ts, kind in ev if kind == "emoji")
        return texts - emojis * ratio >= ratio

    def _face_allowed(self, umo: str) -> bool:
        """黄脸比贴图轻，单独一套闸：间隔更短、每 2 条文字就够配一张。

        原来配脸走的是贴图那套闸（120s 间隔 + 每 5 条文字 1 张），再加上
        auto_face_probability，实际算下来一天也见不到几次——群友的观感就是
        「这人从来不发黄脸」。表情只有几 KB，没必要跟大图共用配额。
        """
        gap = float(self.config.get("face_min_gap_seconds", 45))
        if gap > 0 and time.time() - float(self._last_emit.get(umo, 0.0)) < gap:
            return False
        ratio = int(self.config.get("face_text_per_face", 2))
        if ratio <= 0:
            return True
        window = float(self.config.get("sticker_density_window_seconds", 600))
        cutoff = time.time() - window
        ev = [e for e in self._density.get(umo, []) if e[0] >= cutoff]
        texts = sum(1 for _ts, kind in ev if kind == "text")
        emojis = sum(1 for _ts, kind in ev if kind == "emoji")
        return texts - emojis * ratio >= ratio

    def _note_text(self, umo: str) -> None:
        """记一条「大肥鱼发了句话」，密度闸的配额就加一格。"""
        self._note_density(umo, "text")

    def _note_density(self, umo: str, kind: str) -> None:
        bucket = self._density.setdefault(umo, [])
        bucket.append((time.time(), kind))
        keep_from = time.time() - 3600
        while bucket and bucket[0][0] < keep_from:
            bucket.pop(0)

    def _mark_emit(self, umo: str) -> None:
        self._last_emit[umo] = time.time()
        self._note_density(umo, "emoji")
