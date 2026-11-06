# -*- coding: utf-8 -*-
"""分组体检：把库里每张图归到哪个语义组全打出来，人工核对。

分组错了比不分组更糟（「生气」被塞进「开心」组，AI 挑出来的就全是反的），
所以每次改 STICKER_GROUPS 都要跑一遍这个。

用法：
    python tools/test_groups.py            # 全量分组表
    python tools/test_groups.py --dup      # 只看成员数 > 1 的组
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(PLUGIN_DIR)))  # AstrBot 根


def load():
    sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location(
        "sp_main", os.path.join(PLUGIN_DIR, "main.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sp_main"] = mod          # dataclass 注解需要先注册，否则 NoneType.__dict__
    spec.loader.exec_module(mod)
    return mod


def boot(mod):
    pl = mod.StickerPackPlugin.__new__(mod.StickerPackPlugin)
    pl.config = dict(mod.DEFAULT_CONFIG)
    with open(os.path.join(PLUGIN_DIR, "config.json"), encoding="utf-8-sig") as f:
        pl.config.update(json.load(f))
    pl.pack_dir = pl.config["pack_dir"]
    pl.map_path = os.path.join(pl.pack_dir, pl.config["map_file"])
    pl.stickers = {}
    pl._load()
    pl._load_fav()
    pl._ranker_obj = None
    pl._rank_cache = {}
    pl._fallback_cache = {}
    pl._all_images_cache = []
    pl._recent = {}
    pl._use_count = {}
    pl._deficit = []
    pl._group_cache = {}
    return pl


def main() -> int:
    only_dup = "--dup" in sys.argv
    mod = load()
    pl = boot(mod)
    imgs = [n for n, d in pl.stickers.items() if d.get("kind") == "image"]

    groups: dict[str, list[str]] = {}
    for n in imgs:
        groups.setdefault(pl._group_of(n), []).append(n)

    alone = [g for g, m in groups.items() if len(m) == 1 and g.startswith("·")]
    named_alone = [g for g, m in groups.items() if len(m) == 1 and not g.startswith("·")]

    print(f"图片 {len(imgs)} 张 -> 归入 {len(groups)} 组"
          f"（多成员组 {len(groups) - len(alone) - len(named_alone)}，"
          f"规则单成员 {len(named_alone)}，未识别 {len(alone)}）")
    print()
    for g, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if only_dup and len(members) < 2:
            continue
        tag = "  ?" if g.startswith("·") else ""
        print(f"【{g.lstrip('·')}】{len(members)} 张{tag}")
        for m in members:
            d = pl.stickers[m]
            print(f"    {m:<14} {d['meaning'][:40]}")
        print()

    if alone:
        print("未识别（自成一组，不会和别人折叠）:")
        for g in sorted(alone):
            print(f"    {g[1:]:<14} {pl.stickers[g[1:]]['meaning'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
