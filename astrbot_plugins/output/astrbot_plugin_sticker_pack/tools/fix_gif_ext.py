#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「其实是 GIF、却存成 .jpg/.png」的表情改回 .gif 扩展名。

pool 那批是从 QQ 收藏批量导出的，扩展名是按源文件抄的，内容和后缀不符：
PIL 一读是 GIF/17 帧，但发送链路按 .jpg 走 base64，到 QQ 只剩首帧 ——
这就是「明明是动图，发出去是静态图」的根因。

用法：
    python tools/fix_gif_ext.py --dry-run
    python tools/fix_gif_ext.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.normpath(os.path.join(PLUGIN_DIR, "..", ".."))
STICKER_DIR = os.path.join(DATA_DIR, "stickers")
MAP_PATH = os.path.join(STICKER_DIR, "map.txt")


def main() -> int:
    dry = "--dry-run" in sys.argv
    lines = io.open(MAP_PATH, encoding="utf-8").read().split("\n")
    out: list[str] = []
    renamed: list[tuple[str, str]] = []

    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            out.append(raw)
            continue
        parts = s.split(":")
        if len(parts) < 3 or (len(parts) >= 4 and parts[2] == "face"):
            out.append(raw)
            continue
        rel = parts[2]
        ext = os.path.splitext(rel)[1].lower()
        if ext == ".gif":
            out.append(raw)
            continue
        fp = os.path.join(STICKER_DIR, rel)
        if not os.path.exists(fp):
            out.append(raw)
            continue
        try:
            with Image.open(fp) as im:
                real_gif = im.format == "GIF" or int(getattr(im, "n_frames", 1) or 1) > 1
        except Exception:
            real_gif = False
        if not real_gif:
            out.append(raw)
            continue
        new_rel = os.path.splitext(rel)[0] + ".gif"
        renamed.append((rel, new_rel))
        out.append(":".join(parts[:2] + [new_rel] + parts[3:]))
        if not dry:
            dst = os.path.join(STICKER_DIR, new_rel)
            if not os.path.exists(dst):
                shutil.move(fp, dst)
            else:
                os.remove(fp)

    print(f"需改扩展名：{len(renamed)} 个")
    for a, b in renamed[:10]:
        print(f"  {a} -> {b}")
    if dry:
        print("[dry-run] 未改动")
        return 0
    bak = MAP_PATH + f".bak_gifext_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(MAP_PATH, bak)
    io.open(MAP_PATH, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    print(f"map.txt 已更新，备份 {os.path.basename(bak)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
