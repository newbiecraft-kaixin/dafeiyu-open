"""表情映射回归：给定一批典型群聊句子，打印会配哪张脸、哪些 emoji 会被换掉。

改完 EMOJI_TO_FACE / FACE_MOOD_RULES 后跑一次，肉眼确认没有配到离谱的脸
（比如「不开心」配了呲牙、「尊嘟假嘟」配了大笑）。

用法（务必剥离代理）：
  cd <仓库根>
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \\
      no_proxy='*' NO_PROXY='*' \\
      ./astrbot-win/venv/Scripts/python.exe \\
      <AstrBot 目录>/data/plugins/astrbot_plugin_sticker_pack/tools/test_faces.py
"""

from __future__ import annotations

import ast
import io
import json
import os
import random
import re
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACES_PY = os.path.join(PLUGIN_DIR, "faces.py")
CATALOG = os.path.join(PLUGIN_DIR, "face_catalog.json")
DATA_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))
MAP_TXT = os.path.join(DATA_DIR, "stickers", "map.txt")
PLACEHOLDER_RE = re.compile(r":([^:\s]{1,12}):")

SAMPLES = [
    "今天不开心，别理我",
    "笑死我了这也太离谱了哈哈哈",
    "尊嘟假嘟，你认真的？",
    "我酸了，凭什么他能抽到",
    "裂开了，这bug改了一天",
    "贴贴，我好想你",
    "困了，先睡啦晚安",
    "这也太6了，666",
    "别说了，我不想听",
    "擦汗，差点社死",
    "没事的，我挺好的",
    "懂了，明白了，收到",
    "想开了，随他去吧",
    "饿了，晚上吃啥",
    "干杯！庆祝一下",
    "戳一戳，在吗",
    "偷看一眼，你们在聊啥",
    "我太难了，太南了",
    "好棒，真棒，优秀",
    "恭喜恭喜，终于上岸了",
    "拜拜，我走了",
    "😂😭😡 三连",
    "👍🏻 强！",
    "😍🥰 好可爱",
    "😏🙄 你懂的",
    "🤔❓ 什么意思",
    "🍉🍺☕ 安排",
    "🌹🎉🧧 恭喜发财",
    "😴💤 睡了",
    "🙏🙏 拜托了",
]


def load_consts() -> dict:
    tree = ast.parse(io.open(FACES_PY, encoding="utf-8").read())
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                out[node.target.id] = ast.literal_eval(node.value)
            except ValueError:
                pass
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        out[t.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    return out


def main() -> int:
    random.seed(20261029)
    consts = load_consts()
    emoji_map = dict(consts.get("EMOJI_TO_FACE") or {})
    rules = consts.get("FACE_MOOD_RULES") or []
    substitute = consts.get("FACE_SUBSTITUTE") or {}

    catalog = json.load(io.open(CATALOG, encoding="utf-8"))
    if catalog.get("emoji"):
        emoji_map = {**emoji_map, **catalog["emoji"]}
    allow = set(catalog.get("allow") or [])

    faces: set[str] = set()
    for line in io.open(MAP_TXT, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 4 and parts[2] == "face":
            faces.add(parts[0])

    def face_ok(name: str) -> bool:
        return name in faces and name in allow

    def pick_face(text: str) -> str:
        for words, fs in rules:
            if any(w in text for w in words):
                pool = [f for f in fs if face_ok(f)]
                if pool:
                    return random.choice(pool)
        return ""

    def preconvert(text: str) -> str:
        hits = [(e, n) for e, n in emoji_map.items() if e in text and face_ok(n)]
        out = text
        for e, n in hits:
            out = out.replace(e, f":{n}:")
        # 与 _preconvert 末尾一致：清掉肤色修饰符 / 变体选择符
        out = re.sub(r"[\ufe0f\U0001F3FB-\U0001F3FF]", "", out)
        return out, hits

    print(f"emoji 映射 {len(emoji_map)} 条 / 规则 {len(rules)} 组 / 白名单 {len(allow)} 张\n")
    print(f"{'原句':<22} {'换成':<14} {'配脸':<10} emoji 命中")
    print("-" * 88)
    bad = 0
    for text in SAMPLES:
        conv, hits = preconvert(text)
        # 与 _pick_semantic_face 一致：占位符里的表情名不算正文情绪
        clean = PLACEHOLDER_RE.sub(" ", conv)
        face = pick_face(clean)
        if face and not face_ok(face):
            bad += 1
        mark = " ← 冷门脸!" if face and not face_ok(face) else ""
        hs = ",".join(f"{e}->{n}" for e, n in hits) or "-"
        shown = conv if conv != text else text
        print(f"{text:<22} {shown:<14} {face or '(不配)':<10} {hs}{mark}")

    print()
    # 冷门脸不得漏网
    leak = [n for n in allow if n in substitute]
    print(f"白名单里出现冷门脸(应只在替身表里): {leak or '无'}")
    print(f"非法脸引用: {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
