"""stickers 索引加载（主库 / 收藏库 / 超级表情）。"""

from __future__ import annotations

import os

from astrbot.api import logger

from .faces import FACE_CATALOG, SUPER_FACE_MEANINGS
from .ranker import EMOTION_LEXICON


class IndexMixin:
    # ---------- 索引加载 ----------
    @staticmethod
    def _parse_line(line: str) -> tuple[str, str, str]:
        parts = [p.strip() for p in line.split(":")]
        name = parts[0]
        if len(parts) >= 3:
            return name, parts[1], ":".join(parts[2:])
        return name, "", ":".join(parts[1:])

    def _load(self) -> None:
        self.stickers.clear()
        if not os.path.exists(self.map_path):
            logger.warning(f"[sticker_pack] 索引文件不存在: {self.map_path}")
            return
        with open(self.map_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(":")]
                if len(parts) < 2:
                    continue
                name = parts[0]
                if len(parts) >= 3:
                    meaning, rel = parts[1], ":".join(parts[2:])
                else:
                    meaning, rel = "", ":".join(parts[1:])
                path = rel if os.path.isabs(rel) else os.path.join(self.pack_dir, rel)
                if rel.startswith("face:"):
                    # QQ 官方小黄脸：名称:意思:face:ID
                    try:
                        self.stickers[name] = {
                            "meaning": meaning,
                            "kind": "face",
                            "value": int(rel.split(":", 1)[1]),
                        }
                    except ValueError:
                        logger.warning(f"[sticker_pack] face ID 非法，跳过: {line}")
                    continue
                if not os.path.exists(path):
                    logger.warning(f"[sticker_pack] 图片缺失，跳过: {name} -> {path}")
                    continue
                # 图片条目不覆盖已有的黄脸条目：颜文字映射优先用官方小黄脸（gif 是后加的重复项）
                if name in self.stickers and self.stickers[name].get("kind") == "face":
                    continue
                self.stickers[name] = {"meaning": meaning, "kind": "image", "value": path}
        logger.info(
            f"[sticker_pack] 索引载入完成，共 {len(self.stickers)} 个表情包；根目录 {self.pack_dir}"
        )

    def _load_fav(self) -> int:
        """收藏库单独一份 map，条目带 source=fav，主库同名条目优先。"""
        p = self._fav_map_path()
        if not os.path.exists(p):
            return 0
        n = 0
        with open(p, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                name, meaning, rel = self._parse_line(line)
                if not name or name in self.stickers:
                    continue
                path = rel if os.path.isabs(rel) else os.path.join(self.pack_dir, rel)
                if not os.path.exists(path):
                    logger.warning(f"[sticker_pack] 收藏图缺失，跳过: {name} -> {path}")
                    continue
                self.stickers[name] = {
                    "meaning": meaning, "kind": "image", "value": path, "source": "fav",
                }
                n += 1
        return n

    def _load_super_faces(self) -> int:
        """把 QQ 超级表情（大表情）挂进同一张表情表，kind="super"。

        以前它们只躺在 face_catalog.json 里、被当成「不发」处理，结果只有
        send_super_face 这种点名工具能发出去。这里让它们和表情包一起进
        「名字 + 含义」文字索引，补表情推荐时同池竞争、同样单独发一条
        （单条纯 face 在 QQ 里就是放大的超级表情）。
        """
        if not self.config.get("super_faces_enabled", True):
            return 0
        n = 0
        for item in FACE_CATALOG.get("sysface_hidden") or []:
            name = str(item.get("name") or "").strip()
            try:
                fid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if not name:
                continue
            # 「晚安」「开心」图库里已有同名表情包：加后缀区分，别把图顶掉
            key = name if name not in self.stickers else f"{name}(超级)"
            self.stickers[key] = {
                "meaning": SUPER_FACE_MEANINGS.get(name, name),
                "kind": "super",
                "value": fid,
                "pack": item.get("pack") or "",
            }
            n += 1
        logger.info(f"[sticker_pack] 超级表情并入推荐池：{n} 个")
        return n

    def _build_index(self) -> None:
        """索引里放的是「情绪词表」，不是图名。

        AI 本来就看不到池子（图名一个都不给它），让它看见名字只会造成两个问题：
        小模型照着抄名字、抄错就丢占位符；且它永远只抄清单里那几十个。
        这里只告诉它「可以写哪些情绪词」，具体抽哪张图交给文字相似度排序。
        """
        self.index_text = "、".join(EMOTION_LEXICON.keys())
        logger.info(
            f"[sticker_pack] 情绪词表就绪（{len(EMOTION_LEXICON)} 组）；"
            f"可推荐图片 {len(self._all_images())} 张"
            f"（主库 {sum(1 for v in self.stickers.values() if v.get('source') != 'fav' and v.get('kind') == 'image')}"
            f" + 收藏 {sum(1 for v in self.stickers.values() if v.get('source') == 'fav')}）"
        )
