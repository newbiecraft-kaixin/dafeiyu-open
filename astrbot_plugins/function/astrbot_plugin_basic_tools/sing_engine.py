"""唱歌引擎（纯逻辑层）：简谱解析 + 填词校验。

唱歌 = 按乐谱的旋律唱歌词，不是放歌曲文件，也不是把歌词念出来。
重活（整句 TTS + rubberband 保共振峰变调卡拍 + 交叉淡化补帧）在 sing_render.py，用插件 venv 的 python 跑；
这里只做不依赖音频库的事（简谱解析 / 字数校验 / 组 job），方便离线单测。
"""

from __future__ import annotations

import re

# 简谱 1~7 在 C 大调里对应的半音（相对 C4=60）
_DEGREE_SEMITONE = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}

# 内置旋律库：一行为一句，1~7 是音级，' 升八度、, 降八度，- 延长一拍，0 休止
SCORES: dict[str, dict] = {
    "小星星": {
        "score": (
            "1 1 5 5 6 6 5 - | 4 4 3 3 2 2 1 - | 5 5 4 4 3 3 2 - | "
            "5 5 4 4 3 3 2 - | 1 1 5 5 6 6 5 - | 4 4 3 3 2 2 1 -"
        ),
        "demo": "一闪一闪亮晶晶|满天都是小星星|挂在天上放光明|好像许多小眼睛|一闪一闪亮晶晶|满天都是小星星",
        "mood": "开心",
    },
    "两只老虎": {
        "score": (
            "1 2 3 1 | 1 2 3 1 | 3 4 5 - | 3 4 5 - | "
            "5 6 5 4 3 1 | 5 6 5 4 3 1 | 1 5 1 - | 1 5 1 -"
        ),
        "demo": "两只老虎|两只老虎|跑得快|跑得快|一只没有眼睛|一只没有尾巴|真奇怪|真奇怪",
        "mood": "开心",
    },
    "生日歌": {
        "score": (
            "5 5 6 5 1' 7 | 5 5 6 5 2' 1' | 5 5 5' 3' 2' 1' | "
            "4 4 3' 2' 1' 2'"
        ),
        "demo": "祝你生日快乐|祝你生日快乐|祝你生日快乐|祝你永远快乐",
        "mood": "开心",
    },
    "欢乐颂": {
        "score": (
            "3 3 4 5 5 4 3 2 1 1 2 3 3 2 2 - | "
            "3 3 4 5 5 4 3 2 1 1 2 3 2 1 1 -"
        ),
        "demo": "欢乐女神圣洁美丽灿烂光芒照大地|我们心中充满热情来到你的圣殿里",
        "mood": "开心",
    },
}

_TOKEN_RE = re.compile(r"[1-7][',]*|0|[-|]")


def parse_score(score: str) -> list[dict]:
    """把简谱文本拆成「按 | 分句」的音符列表。

    返回 [{"notes": [(midi, 拍数) ...], "beats": 总拍数, "syllables": 要唱几个字}]
    - 每个 1~7 = 一个字（唱一个音节）
    - `-` 给前一个音符加一拍
    - `0` 是休止一拍（不唱字）
    """
    text = str(score or "").replace("｜", "|")
    lines = [ln for ln in re.split(r"[|\n]", text) if ln.strip()]
    out: list[dict] = []
    for ln in lines:
        notes: list[list] = []  # [midi, beats]
        for tok in _TOKEN_RE.findall(ln):
            if tok == "-":
                if notes:
                    notes[-1][1] += 1
                continue
            if tok == "0":
                notes.append([None, 1])  # 休止
                continue
            digit = int(tok[0])
            midi = 60 + _DEGREE_SEMITONE[digit]
            up = tok.count("'") - tok.count(",")
            midi += 12 * up
            notes.append([midi, 1])
        if not notes:
            continue
        beats = sum(n[1] for n in notes)
        syllables = sum(1 for n in notes if n[0] is not None)
        out.append({"notes": notes, "beats": beats, "syllables": syllables})
    return out


def pick_score(name: str) -> dict:
    """按名字挑内置旋律（模糊匹配），挑不到就用小星星。"""
    found = find_score(name)
    if found:
        return found
    return {"name": "小星星", **SCORES["小星星"]}


def find_score(name: str) -> dict | None:
    """按名字找内置旋律；找不到返回 None（调用方去扒 mp3 的旋律）。"""
    want = str(name or "").strip()
    if not want:
        return None
    for key, item in SCORES.items():
        if want == key or want in key or key in want:
            return {"name": key, **item}
    return None


def score_summary(sc: dict) -> tuple[list[int], list[dict]]:
    """返回（每句字数, 解析后的乐谱行）。"""
    lines = parse_score(sc["score"])
    return [ln["syllables"] for ln in lines], lines


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def count_han(text: str) -> int:
    return len(_HAN_RE.findall(str(text or "")))


def normalize_lyrics(lines: list[str], patterns: list[int]) -> list[str] | None:
    """校验歌词：行数一致、每行汉字数等于音符数；不合格返回 None。"""
    if not lines or len(lines) != len(patterns):
        return None
    out: list[str] = []
    for ln, n in zip(lines, patterns):
        k = count_han(ln)
        if k != n:
            return None
        out.append(ln.strip())
    return out


def fit_lyrics(lines: list[str], patterns: list[int]) -> list[str] | None:
    """尽量原样保留用户歌词，按旋律每一句对齐用户的每一行。

    行数/字数对不上时，不丢给模型重编：第 i 个旋律句对应第 i 行用户词
    （行不够就循环复用，像副歌唱两遍）；每句多出的字截掉，少的用本行字循环补齐。
    这样词是整句整句地落在旋律上，不会出现「第一次开 / 嗓尾巴…」这种拦腰切。
    """
    src = [[c for c in _HAN_RE.findall(str(ln or ""))] for ln in lines]
    src = [s for s in src if s]
    if not src:
        return None
    out: list[str] = []
    for i, n in enumerate(patterns):
        s = list(src[i % len(src)])
        if len(s) < n:
            base = list(s)
            k = 0
            while len(s) < n:
                s.append(base[k % len(base)])
                k += 1
        out.append("".join(s[:n]))
    return out


def build_job(sc: dict, lyrics: list[str]) -> dict:
    """拼成 sing_render.py 吃的任务：每句 = 文本 + 音符（midi/拍数）。"""
    patterns, lines = score_summary(sc)
    job_lines = []
    for ln, text in zip(lines, lyrics):
        job_lines.append({
            "text": text,
            "notes": [[n[0], n[1]] for n in ln["notes"]],
            "beats": ln["beats"],
        })
    return {"song": sc["name"], "lines": job_lines}
