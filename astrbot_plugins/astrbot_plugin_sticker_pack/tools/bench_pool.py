"""推荐池回归基准：用真实群聊语料量化「补表情」候选池的质量。

改完排名/兜底逻辑后跑一次，对比四个指标：
  - 空池率：应为 0（空池 = 这条表情直接报废，是最严重的退化）
  - 满池率：候选凑满 follow_up_candidates 的比例
  - 多样性：同一句话多次抽取得到的组合数
  - 描述覆盖：候选必须带 meaning，否则 AI 无法判断贴不贴切

用法（务必剥离代理）：
  cd <仓库根>
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
      no_proxy='*' NO_PROXY='*' \
      ./astrbot-win/venv/Scripts/python.exe \
      <AstrBot 目录>/data/plugins/astrbot_plugin_sticker_pack/tools/bench_pool.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import sqlite3
import sys
from collections import Counter

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.dirname(os.path.dirname(PLUGIN_DIR))
ROOT = os.path.dirname(DATA)
sys.path.insert(0, ROOT)

SAMPLES = 120
ROUNDS = 8
DIVERSITY_CASES = 5


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "sp", os.path.join(PLUGIN_DIR, "main.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sp"] = mod
    spec.loader.exec_module(mod)

    cfg = json.load(open(os.path.join(PLUGIN_DIR, "config.json"), encoding="utf-8-sig"))
    obj = mod.StickerPackPlugin.__new__(mod.StickerPackPlugin)
    obj.config = dict(mod.DEFAULT_CONFIG)
    obj.config.update(cfg)
    obj.pack_dir = os.path.join(DATA, "stickers")
    obj.map_path = os.path.join(obj.pack_dir, obj.config.get("map_file", "map.txt"))
    obj.stickers = {}
    obj.index_text = ""
    obj._state_path = os.path.join(PLUGIN_DIR, "_append_state.json")
    obj._state = {}
    obj._follow_rate = []
    obj._last_follow = {}
    obj._follow_running = set()
    obj._solo_tasks = set()
    obj._bots = {}
    obj._self_ids = {}
    obj._recent = {}
    obj._face_recent = {}
    obj._use_count = {}
    obj._ranker_obj = None
    obj._rank_cache = {}
    obj._fallback_cache = {}
    obj._all_images_cache = []
    obj._dhash_map = {}
    obj._pending = {}
    obj._deficit = []
    obj._mon_task = None
    obj._hash_task = None
    obj._follow_stats = dict.fromkeys(
        ["pass", "sent", "block_prob", "block_cap", "block_running",
         "block_cool", "no_pick", "pool_empty", "send_fail"], 0
    )
    obj._load()
    obj._load_fav()
    obj._build_index()
    obj._group_cache = {}
    obj._build_ranker()
    return obj


def real_sentences(limit: int) -> list[str]:
    db_path = os.path.join(
        DATA, "plugin_data", "astrbot_plugin_livingmemory", "conversations.db"
    )
    if not os.path.isfile(db_path):
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT content FROM messages WHERE session_id LIKE '%GroupMessage%' "
        "AND length(content) BETWEEN 3 AND 60 ORDER BY timestamp DESC LIMIT ?",
        (limit * 4,),
    ).fetchall()
    db.close()
    out = []
    for (c,) in rows:
        t = str(c or "").strip()
        if not t or t.startswith("[") or t.startswith("/"):
            continue
        out.append(t)
    random.shuffle(out)
    return out[:limit]


def main() -> int:
    p = load_plugin()
    n = int(p.config.get("follow_up_candidates", 5))
    print(f"索引 {len(p.stickers)} 条 | 可推荐图片 {len(p._all_images())} | 候选数 n={n}")

    samples = real_sentences(SAMPLES)
    if not samples:
        print("没取到真实语料，跳过")
        return 1

    sizes, empty = [], []
    for t in samples:
        c = p._match_candidates(t, n, "")
        sizes.append(len(c))
        if not c:
            empty.append(t)
    order = sorted(sizes)
    total = len(sizes)
    print(f"\n样本 {total} 句")
    print(f"  空池率   {len(empty)/total:>6.1%}   （目标 0%）")
    print(f"  满池率   {sum(1 for s in sizes if s >= n)/total:>6.1%}   （目标 100%）")
    print(f"  候选数   min={order[0]} p25={order[total//4]} "
          f"中位={order[total//2]} p75={order[total*3//4]} max={order[-1]}")
    print(f"  分布     {dict(sorted(Counter(sizes).items()))}")

    cases = (empty or samples)[:DIVERSITY_CASES]
    print(f"\n多样性（每句抽 {ROUNDS} 次）")
    for q in cases:
        combos, names = set(), []
        for _ in range(ROUNDS):
            c = p._match_candidates(q, n, "")
            combos.add(tuple(sorted(c)))
            names.extend(c)
        top = Counter(names).most_common(2)
        print(f"  「{q[:20]}」不同组合 {len(combos)}/{ROUNDS} | 最高频 {top}")
    # 同一句里不应出现重复图
    dup = 0
    for q in samples[:40]:
        c = p._match_candidates(q, n, "")
        if len(c) != len(set(c)):
            dup += 1
    print(f"\n候选内部重复的句子: {dup} 句（目标 0）")

    no_desc = 0
    for q in samples[:40]:
        for name in p._match_candidates(q, n, ""):
            if not (p.stickers.get(name, {}).get("meaning") or "").strip():
                no_desc += 1
    print(f"无描述候选: {no_desc} 个（目标 0，否则 AI 无法判断）")
    return 0 if not empty and dup == 0 and no_desc == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
