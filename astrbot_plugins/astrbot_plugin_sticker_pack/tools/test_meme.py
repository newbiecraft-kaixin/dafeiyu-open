#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""斗图能力自测：不连接 QQ，直接把插件的选图逻辑跑一遍。

两条真实链路都测：
  A. send_meme（AI 主动要图）：传一个情绪词 -> 内部取候选第一张
  B. 补表情（AI 说完话后系统配图）：传整句 -> _match_candidates

同时验证：空池率、首选集中度、30 秒间隔闸、单次只出一张。

    python tools/test_meme.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.normpath(os.path.join(PLUGIN_DIR, "..", ".."))
ROOT = os.path.normpath(os.path.join(DATA_DIR, ".."))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location(
    "sp_main", os.path.join(PLUGIN_DIR, "main.py")
)
mod = importlib.util.module_from_spec(spec)
sys.modules["sp_main"] = mod          # dataclass / 注解解析需要它先注册
spec.loader.exec_module(mod)

Plugin = mod.StickerPackPlugin


def build():
    p = Plugin.__new__(Plugin)
    with open(os.path.join(PLUGIN_DIR, "config.json"), encoding="utf-8") as f:
        conf = json.load(f)
    p.config = {**mod.DEFAULT_CONFIG, **conf}
    p.pack_dir = p.config["pack_dir"]
    p.map_path = os.path.join(p.pack_dir, p.config["map_file"])
    p.stickers = {}
    p._load()
    try:
        p._load_fav()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 收藏库加载跳过: {type(e).__name__}: {e}")
    p._ranker_obj = None
    p._rank_cache = {}
    p._fallback_cache = {}
    p._all_images_cache = []
    p._recent = {}
    p._use_count = {}
    p._deficit = []
    p._group_cache = {}
    p._dhash_map = {}
    p._pending = {}
    p._face_recent = {}
    p._last_emit = {}
    p._last_follow = {}
    p._follow_rate = []
    p._follow_running = set()
    p._follow_stats = dict.fromkeys(
        ["pass", "sent", "block_prob", "block_cap", "block_running",
         "block_cool", "block_gap", "no_pick", "pool_empty", "send_fail"], 0
    )
    return p


MOODS = [
    "开心", "笑死", "无语", "震惊", "委屈", "累了", "饿了", "得意",
    "生气", "害羞", "摆烂", "收到", "阴阳", "被夸了有点飘", "看到傻子言论",
]

SENTENCES = [
    "你是傻子吧，这都信",
    "哈哈哈笑死我了这也太离谱",
    "就这？我还以为多厉害呢",
    "我考了满分，请叫我学霸",
    "救命啊老板又让加班到十点",
    "晚安，我先睡了啊",
    "在吗，出来挨打",
    "你请我喝奶茶吧",
    "我失恋了，好难过",
    "发工资了，今晚我请",
    "你行你上啊",
    "今天热死了，四十度",
    "我不理你了，哼",
    "恭喜恭喜，终于上岸了",
    "老板来了，快装忙",
    "呵呵，随便你",
    "累成咸鱼了，不想动",
    "你猜我为什么生气",
    "别笑了，认真点",
    "这瓜真甜，还有后续吗",
]


def main() -> int:
    p = build()
    imgs = [n for n, v in p.stickers.items() if v.get("kind") == "image"]
    print(f"库内图片 {len(imgs)} 张 / 全部条目 {len(p.stickers)} 条")
    print()

    umo = "大肥鱼QQ:GroupMessage:1000000001"

    print("=" * 74)
    print("A. send_meme：AI 传情绪词 -> 直接发候选第一张")
    print("=" * 74)
    first_a: dict[str, int] = {}
    empty = 0
    for m in MOODS:
        names = p._match_candidates(m, 3, umo, allow_fallback=False, min_score=0.85)
        if not names:
            empty += 1
            print(f"{m:<12} -> （空池，发不出）")
            continue
        pick = names[0]
        first_a[pick] = first_a.get(pick, 0) + 1
        print(f"{m:<12} -> {pick:<10}｜{p.stickers[pick]['meaning'][:34]}")
        print(f"{'':<12}    备选：{'、'.join(names[1:]) or '无'}")
    print()

    print("=" * 74)
    print("B. 补表情：整句进 -> 候选前三")
    print("=" * 74)
    first_b: dict[str, int] = {}
    empty_b = 0
    for s in SENTENCES:
        # 真实链路：follow_up_candidates=5 张候选交给 AI 挑
        names = p._match_candidates(s, 5, umo, min_score=0.5)
        if not names:
            empty_b += 1
            print(f"{s[:20]:<22} -> （空池，不发）")
            continue
        pick = names[0]
        first_b[pick] = first_b.get(pick, 0) + 1
        shows = "、".join(f"{n}" for n in names)
        print(f"{s[:20]:<22} -> 候选 {len(names)}：{shows}")
        print(f"{'':<22}    {'/'.join(p.stickers[n]['meaning'][:12] for n in names)}")
    print()

    print("=" * 74)
    print("C. 分布与闸门")
    print("=" * 74)
    all_first = list(first_a) + list(first_b)
    uniq = len(set(all_first))
    print(f"空池次数：send_meme {empty}/{len(MOODS)}，补表情 {empty_b}/{len(SENTENCES)}"
          f"（空池 = 主动不发，不是故障）")
    print(f"A 组首选去重：{uniq if False else len(first_a)} 种 / {len(MOODS)} 次")
    print(f"B 组首选去重：{len(first_b)} 种 / {len(SENTENCES)} 次")
    print(f"两组首选合计去重：{len(set(all_first))} 种 / {len(all_first)} 次")
    top = sorted(
        {**{k: first_a.get(k, 0) + first_b.get(k, 0) for k in set(first_a) | set(first_b)}}.items(),
        key=lambda x: -x[1],
    )[:5]
    print("最常被选中：" + "、".join(f"{k}×{v}" for k, v in top))

    # 30 秒间隔闸
    p._last_emit.pop(umo, None)
    ok1 = p._emit_allowed(umo)
    p._mark_emit(umo)
    ok2 = p._emit_allowed(umo)
    p._last_emit[umo] = time.time() - 31
    ok3 = p._emit_allowed(umo)
    print(f"间隔闸：首次 {ok1} / 立刻第二次 {ok2} / 隔 31 秒 {ok3}"
          f"（期望 True / False / True）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
