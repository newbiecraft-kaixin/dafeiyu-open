"""图片多特征指纹、查重与指纹缓存维护。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from PIL import Image as PILImage
from astrbot.api import logger

# 图片指纹边长：16 -> 256 bit。
# 实测 64 bit 时「同一张图重压缩/缩半」最差只有 0.891，会漏放重复图；
# 256 bit 同一张图最差 0.930（判重），不同图最高 0.734（放行），正好卡在 0.90 阈值两侧。
DHASH_SIZE = 16
AHASH_SIZE = 16
# 颜色直方图边长：32x32 像素量化成 4x4x4=64 个颜色桶，补 dHash/aHash 只看灰度的短板
# （同一张图被调了色/加了滤镜后灰度梯度几乎不变，但色块分布会变，靠这一项兜住）。
COLOR_SIDE = 32
# 综合相似度权重：md5 相同直接算 1.0；否则按 dHash / aHash / 直方图加权。
FP_WEIGHTS = (("d", 0.45), ("a", 0.25), ("c", 0.30))
IMG_EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _bits_sim(a: str, b: str, total: int) -> float:
    """两个等长 hex 指纹的逐位相似度；缺一项返回 -1（不参与加权）。"""
    if not a or not b:
        return -1.0
    try:
        diff = int(a, 16) ^ int(b, 16)
    except ValueError:
        return -1.0
    return 1.0 - bin(diff).count("1") / float(total)


def _hist_sim(a, b) -> float:
    """颜色直方图余弦相似度；缺一项返回 -1。"""
    if not a or not b:
        return -1.0
    try:
        a = [float(x) for x in a]
        b = [float(x) for x in b]
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
    except (TypeError, ValueError):
        return -1.0
    if na <= 0 or nb <= 0:
        return -1.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _fp_similarity(a, b) -> float:
    """两张图的综合相似度（0~1）。

    内容哈希（md5/dHash）只能拦住「字节或灰度完全一致」的那部分重复；
    同一张图被裁剪、调色、重压缩、换一帧就失效，所以这里叠加 aHash 与颜色直方图。
    旧缓存只存 dHash 字符串时，自动退化为单特征比较。
    """
    if not a or not b:
        return 0.0
    if isinstance(a, str):
        a = {"d": a}
    if isinstance(b, str):
        b = {"d": b}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return 0.0
    if a.get("m") and b.get("m") and a["m"] == b["m"]:
        return 1.0
    total = DHASH_SIZE * DHASH_SIZE
    acc = wsum = 0.0
    for key, w in FP_WEIGHTS:
        if key in ("d", "a"):
            s = _bits_sim(a.get(key), b.get(key), total)
            if key == "d":
                # 边框被裁过（去水印/白边）时整图 dHash 会整体错位，
                # 再比一次「中心 92% 区域」的 dHash，取高的那个
                s2 = _bits_sim(a.get("d2"), b.get("d2"), total)
                if s2 > s:
                    s = s2
        else:
            s = _hist_sim(a.get(key), b.get(key))
        if s < 0:
            continue
        acc += s * w
        wsum += w
    return acc / wsum if wsum else 0.0


class FingerprintMixin:
    @staticmethod
    def _path_sig(path: str) -> str:
        """文件签名（mtime:size）：用来判断缓存指纹是否还对应现在这张图。"""
        try:
            st = os.stat(path)
        except OSError:
            return ""
        return f"{int(st.st_mtime)}:{st.st_size}"

    @staticmethod
    def _fingerprint(path: str) -> dict:
        """一张图的多特征指纹：md5 + dHash + aHash + 颜色直方图。动图只取第一帧。

        md5 管「一模一样」；dHash/aHash 管重压缩、缩放；颜色直方图管调色、加滤镜、
        轻微裁剪。都拦不住的情况（大幅改写/换脸）本来也不算同一张图。
        """
        n = DHASH_SIZE
        try:
            st = os.stat(path)
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            md5 = h.hexdigest()
            with PILImage.open(path) as im:
                # GIF/WebP 动图：显式回到第 0 帧。多帧图不取首帧哈希会随帧漂移，
                # 同一张图每次算出来都不一样，查重直接失效。
                im.seek(0)
                rgb = im.convert("RGB")
                gray = rgb.convert("L")
                d_img = gray.resize((n + 1, n), PILImage.Resampling.LANCZOS)
                d_buf = d_img.tobytes()
                # 中心 92% 区域的 dHash：图被裁掉一圈边框时，整图 dHash 会错位，
                # 这一份还能对上，用来兜住「裁剪版=重复」
                w_px, h_px = rgb.size
                cw, ch = max(1, int(w_px * 0.92)), max(1, int(h_px * 0.92))
                x0, y0 = (w_px - cw) // 2, (h_px - ch) // 2
                d2_img = gray.crop((x0, y0, x0 + cw, y0 + ch)).resize(
                    (n + 1, n), PILImage.Resampling.LANCZOS)
                d2_buf = d2_img.tobytes()
                a_img = gray.resize((n, n), PILImage.Resampling.LANCZOS)
                a_buf = a_img.tobytes()
                c_img = rgb.resize((COLOR_SIDE, COLOR_SIDE), PILImage.Resampling.BILINEAR)
                c_buf = c_img.tobytes()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 指纹计算失败: {type(e).__name__}: {e}")
            return {}
        bits = 0
        d2bits = 0
        stride = n + 1
        for r in range(n):
            base = r * stride
            for c in range(n):
                bits = (bits << 1) | (1 if d_buf[base + c] > d_buf[base + c + 1] else 0)
                d2bits = (d2bits << 1) | (1 if d2_buf[base + c] > d2_buf[base + c + 1] else 0)
        mean = sum(a_buf) / max(1, len(a_buf))
        abits = 0
        for v in a_buf:
            abits = (abits << 1) | (1 if v > mean else 0)
        hist = [0] * 64
        for i in range(0, len(c_buf) - 2, 3):
            hist[(c_buf[i] >> 6) * 16 + (c_buf[i + 1] >> 6) * 4 + (c_buf[i + 2] >> 6)] += 1
        return {
            "m": md5,
            "d": f"{bits:0{n * n // 4}x}",
            "d2": f"{d2bits:0{n * n // 4}x}",
            "a": f"{abits:0{AHASH_SIZE * AHASH_SIZE // 4}x}",
            "c": hist,
            "sig": f"{int(st.st_mtime)}:{st.st_size}",
        }

    async def _warm_hashes(self) -> None:
        """后台算全库指纹并落盘缓存：查重要跟几百张图比，不能在消息线程里现算。"""
        try:
            await asyncio.sleep(4)
            await self._refresh_hashes()
            # 启动时顺手清一次过期文件，避免重启后旧图继续堆着
            self._cleanup_stale_files()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 指纹预热失败: {type(e).__name__}: {e}")

    async def _refresh_hashes(self) -> None:
        """重算指纹表（主库 + 收藏库 + 待核验区），外部改动的图也能跟上。"""
        try:
            items = [(n, it.get("value") or "") for n, it in list(self.stickers.items())
                     if it.get("kind") == "image"]
            table = await asyncio.to_thread(self._build_hash_table, items)
            self._dhash_map = table
            self._save_hash_cache()
            review_n = sum(1 for k in table if k.startswith("@review:"))
            logger.info(
                f"[sticker_pack] 表情指纹就绪 {len(table)} 张"
                f"（含待核验 {review_n} 张，查重阈值 {self.config.get('fav_similarity_max', 0.9)}）"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 指纹重算失败: {type(e).__name__}: {e}")

    def _build_hash_table(self, sticker_items: list) -> dict:
        """同步构建指纹表（放线程池跑）。

        键约定：库内图用名字；待核验区的图用 ``@review:<文件名>``。
        只有文件签名（mtime:size）没变的条目才复用缓存，避免算一整遍。
        """
        cache: dict = {}
        p = self._hash_cache_path()
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cache = json.load(f) or {}
            except (OSError, ValueError):
                cache = {}
        else:
            # 旧版 _dhash_v2.json 只有 dHash 字符串，一次性迁移成多特征结构
            legacy = os.path.join(self._fav_dir(), "_dhash_v2.json")
            if os.path.exists(legacy):
                try:
                    with open(legacy, encoding="utf-8") as f:
                        old = json.load(f) or {}
                    for k, v in old.items():
                        cache[k] = {"d": v} if isinstance(v, str) else v
                except (OSError, ValueError):
                    cache = {}
        targets: dict[str, str] = {}
        for name, path in sticker_items:
            if path:
                targets[name] = path
        try:
            names = os.listdir(self._review_dir())
        except OSError:
            names = []
        for fn in names:
            if os.path.splitext(fn)[1].lower() not in IMG_EXTS:
                continue
            targets["@review:" + fn] = os.path.join(self._review_dir(), fn)
        keep: dict = {}
        for key, path in targets.items():
            ent = cache.get(key)
            sig = self._path_sig(path)
            if sig and isinstance(ent, dict) and ent.get("sig") == sig:
                keep[key] = ent
                continue
            fp = self._fingerprint(path)
            if fp:
                keep[key] = fp
        # 没进 keep 的旧条目（图被删/改名/已出库/已核验入库）直接丢弃
        return keep

    def _find_duplicate(self, fp: dict) -> tuple[str, float]:
        """在已入库/待核验的全部图片里找最像的一张，超过阈值就算重复。"""
        if not fp:
            return "", 0.0
        best_name, best = "", 0.0
        for name, other in self._dhash_map.items():
            sim = _fp_similarity(fp, other)
            if sim > best:
                best, best_name = sim, name
        thr = float(self.config.get("fav_similarity_max", 0.90))
        return (best_name, best) if best > thr else ("", best)

    @staticmethod
    def _fp_label(key: str) -> str:
        """指纹键 -> 人话名字（待核验区只显示 AI 起的短名）。"""
        if key.startswith("@review:"):
            base = os.path.splitext(key.split(":", 1)[1])[0]
            return base.rsplit("_", 1)[0] or "待核验的一张"
        return key

    def _save_hash_cache(self) -> None:
        try:
            os.makedirs(self._fav_dir(), exist_ok=True)
            with open(self._hash_cache_path(), "w", encoding="utf-8") as f:
                json.dump(self._dhash_map, f, ensure_ascii=False)
            # 旧缓存迁移完成后删掉，避免两份指纹互相打架
            legacy = os.path.join(self._fav_dir(), "_dhash_v2.json")
            if os.path.exists(legacy):
                try:
                    os.remove(legacy)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    # 定时清理：过期待核验图、超时/孤儿待确认图，并同步指纹缓存。
    # 主库 map.txt 里的图一律不动（那是人工维护的资产）；
    # 这里只清 AI 自采的临时产物，否则 _review 和 _pending 会无限膨胀。
    def _cleanup_stale_files(self) -> None:
        now = time.time()
        removed_review = 0
        days = float(self.config.get("review_ttl_days", 7) or 0)
        if days > 0:
            ttl = days * 86400
            rdir = self._review_dir()
            try:
                names = os.listdir(rdir)
            except OSError:
                names = []
            for fn in names:
                if os.path.splitext(fn)[1].lower() not in IMG_EXTS:
                    continue
                path = os.path.join(rdir, fn)
                try:
                    if now - os.path.getmtime(path) < ttl:
                        continue
                    os.remove(path)
                except OSError:
                    continue
                self._dhash_map.pop("@review:" + fn, None)
                removed_review += 1
        ttl_p = float(self.config.get("fav_pending_timeout_minutes", 10) or 10) * 60
        pending_changed = False
        for umo, info in list(self._pending.items()):
            if now - float(info.get("ts", 0)) <= ttl_p:
                continue
            self._pending.pop(umo, None)
            try:
                os.remove(info.get("file", ""))
            except OSError:
                pass
            pending_changed = True
        if pending_changed:
            self._save_pending()
        ref = {os.path.normpath(str(i.get("file"))).lower()
               for i in self._pending.values() if i.get("file")}
        removed_pending = 0
        pdir = self._pending_dir()
        try:
            names = os.listdir(pdir)
        except OSError:
            names = []
        for fn in names:
            path = os.path.join(pdir, fn)
            if os.path.normpath(path).lower() in ref:
                continue
            try:
                # 刚落地还没问出含义的文件先留着，超过待确认 TTL 才当孤儿删
                if now - os.path.getmtime(path) <= ttl_p:
                    continue
                os.remove(path)
            except OSError:
                continue
            removed_pending += 1
        if removed_review or removed_pending or pending_changed:
            self._save_hash_cache()
            logger.info(
                f"[sticker_pack] 定时清理：过期待核验 {removed_review} 张，"
                f"孤儿待确认 {removed_pending} 张"
            )
