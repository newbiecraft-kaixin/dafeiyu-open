#!/usr/bin/env python3
"""把桌宠的固定台词预热进中枢 TTS 缓存。

桌宠每句都要现合成一次 CosyVoice（热态 8~20 秒），睡觉/工作状态这类固定台词
反反复复念同一句，现合成既慢又容易把播放队列拖乱（声音出来时字幕已经换了一句）。
这里把它们提前合成好落进 tts_cache，桌宠再念就是秒回。

用法：
    python tts_prewarm.py [声线名]        # 默认「小忆」
    python tts_prewarm.py 小忆 --dry-run  # 只列台词不合成
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(r"C:\Users\KaiXin\.dsh\dsh-pet\main-config.jsonc")
WEBM_DIR = Path(r"C:\Users\KaiXin\.dsh\profiles\web\node_modules\dsh-pet\assets\webm")
HUB_TTS = "http://127.0.0.1:23408/bot/tts"
DEFAULT_VOICE = "小忆"


def _read_config() -> dict:
    raw = CONFIG.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # jsonc 兜底：去掉行注释与尾逗号（字符串内的 // 不做处理，这份配置里没有）
        text = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        return json.loads(text)


def _fixed_lines(conf: dict) -> list[str]:
    """固定台词：睡觉三态 + 工作状态。这些是写死的，每次念都一字不差。"""
    lines: list[str] = []
    for key in ("sleepRefuseTexts", "sleepStartTexts", "sleepEndTexts"):
        lines.extend(str(item) for item in (conf.get(key) or []))
    for row in conf.get("workStatusTexts") or []:
        if isinstance(row, list):
            lines.extend(str(item) for item in row)
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def _action_line(name: str) -> str:
    """与桌宠 lib/index.js 的 visionActionLine 保持一致：点播动作弹什么就预热什么。"""
    raw = name.strip()
    if not raw:
        return "看我的新动作！"
    match = re.match(r"^(余额|碎碎念|工作状态|睡觉|点击回应)[-－—:：]", raw)
    core = raw[match.end():] if match else raw
    if not match:
        line = "看我的——" + core + "！"
    elif match.group(1) == "余额":
        line = "钱包小管家：" + core + "！"
    elif match.group(1) == "工作状态":
        line = "一起加油：" + core
    else:
        line = core
    return line[:50]


def _action_lines() -> list[str]:
    """点播动作的字幕台词：动作名来自 assets/webm 的文件名，是固定集合。"""
    if not WEBM_DIR.is_dir():
        return []
    return [_action_line(path.stem) for path in WEBM_DIR.glob("*.webm")]


def _post(text: str, voice: str) -> tuple[int, int]:
    body = json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        HUB_TTS, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, len(resp.read())


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    voice = args[0] if args else DEFAULT_VOICE
    lines = _fixed_lines(_read_config())
    fixed_count = len(lines)
    actions = _action_lines()
    seen = set(lines)
    for line in actions:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    print(
        f"[prewarm] 声线={voice} 固定台词={fixed_count} 条 "
        f"动作台词={len(actions)} 条 合计={len(lines)} 条"
    )
    if dry:
        for line in lines:
            print("  " + line)
        return 0
    ok = 0
    for index, line in enumerate(lines, 1):
        try:
            status, size = _post(line, voice)
            good = status == 200 and size > 0
            ok += 1 if good else 0
            print(f"[prewarm] {index}/{len(lines)} {'OK' if good else 'FAIL'} {size}B  {line}")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"[prewarm] {index}/{len(lines)} ERROR {error}  {line}")
    print(f"[prewarm] 完成 {ok}/{len(lines)}")
    return 0 if ok == len(lines) else 1


if __name__ == "__main__":
    raise SystemExit(main())
