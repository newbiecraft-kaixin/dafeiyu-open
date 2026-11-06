#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把表情库里「不动帧」的静态图挪出主库，并同步删掉 map.txt 条目。

为什么：静态 jpg/png/webp 发出去就是一张截图，不像表情包；
QQ 里真人斗图发的都是动图。主库只留动图，推荐池更干净。

不真删：文件移到 stickers/_static_archive/<原目录>/，map.txt 先备份，
想回滚直接把 archive 里的文件搬回去、再恢复 map.txt.bak_static 即可。

用法：
    python tools/purge_static.py --dry-run   # 只看会删什么
    python tools/purge_static.py             # 真搬
"""
from __future__ import annotations

import argparse
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
ARCHIVE = os.path.join(STICKER_DIR, "_static_archive")


def frames(path: str) -> int:
    try:
        with Image.open(path) as im:
            return int(getattr(im, "n_frames", 1))
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lines = io.open(MAP_PATH, encoding="utf-8").read().split("\n")
    keep: list[str] = []
    moved: list[tuple[str, str]] = []
    static_names: list[str] = []

    for ln in lines:
        raw = ln.rstrip("\n")
        s = raw.strip()
        if not s or s.startswith("#"):
            keep.append(raw)
            continue
        parts = s.split(":")
        # face 条目原样保留
        if len(parts) >= 4 and parts[2] == "face":
            keep.append(raw)
            continue
        if len(parts) < 3:
            keep.append(raw)
            continue
        name, meaning, rel = parts[0], parts[1], parts[2]
        fp = os.path.join(STICKER_DIR, rel)
        if not os.path.exists(fp):
            keep.append(raw)
            continue
        n = frames(fp)
        if n > 1:
            keep.append(raw)
            continue
        # 静态或读不出帧 -> 出库
        static_names.append(name)
        if not args.dry_run:
            dst = os.path.join(ARCHIVE, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.move(fp, dst)
            moved.append((rel, dst))

    print(f"图片条目总览：静态出库 {len(static_names)} 条，保留 {len([l for l in keep if l.strip() and not l.strip().startswith('#')]) - sum(1 for l in keep if l.strip() and len(l.split(':'))>=4 and l.split(':')[2]=='face')} 条图片")
    print("静态出库样例：", "、".join(static_names[:15]))

    if args.dry_run:
        print("[dry-run] 未改动任何文件")
        return 0

    bak = MAP_PATH + f".bak_static_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(MAP_PATH, bak)
    io.open(MAP_PATH, "w", encoding="utf-8", newline="\n").write("\n".join(keep))
    print(f"map.txt 已重写，备份：{os.path.basename(bak)}")
    print(f"已移动 {len(moved)} 个文件 -> {ARCHIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
