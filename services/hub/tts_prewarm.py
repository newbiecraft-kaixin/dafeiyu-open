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
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 默认取本机用户目录下的 .dsh；可用环境变量 DSH_HOME 覆盖
_DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
CONFIG = _DSH_HOME / "dsh-pet" / "main-config.jsonc"
WEBM_DIR = _DSH_HOME / "profiles" / "web" / "node_modules" / "dsh-pet" / "assets" / "webm"
HUB_TTS = "http://127.0.0.1:23408/bot/tts"
DEFAULT_VOICE = "小忆"
# 桌宠 Relay 播报时会拼「昵称：正文」，这几句固定文案只有带上昵称才命中缓存
CHAT_FIXED_LINES = ("发来一张图片", "收到一条消息")
# human_sim 的 state.json 里 poke_stat:name:<qq> 存着已知昵称，用它拼带前缀的台词
def _human_sim_state() -> Path:
    """HUMAN_SIM_STATE > 安装根下 AstrBot-* 里最近写入的一份（不写死版本号）。"""
    env = os.environ.get("HUMAN_SIM_STATE", "").strip()
    if env:
        return Path(env)
    base = Path(__file__).resolve().parents[2] / "astrbot-win"
    try:
        hits = list(
            base.glob("AstrBot-*/data/plugin_data/astrbot_plugin_human_sim/state.json")
        )
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        hits = []
    return hits[0] if hits else base / "AstrBot" / "data" / "plugin_data" / "astrbot_plugin_human_sim" / "state.json"


HUMAN_SIM_STATE = _human_sim_state()
MAX_NAMES = 12


def _configured_voice(conf: dict) -> str:
    """桌宠实际在用的声线（main-config.jsonc 的 voiceName），没配再退回默认。"""
    return str(conf.get("voiceName") or "").strip() or DEFAULT_VOICE


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


def _known_names(limit: int = MAX_NAMES) -> list[str]:
    """从 human_sim 的 state.json 收集已知昵称。

    Relay 播报的文本是「昵称：正文」，只预热纯台词永远命中不了缓存，
    所以固定文案要按「昵称：台词」再预热一遍。
    """
    try:
        with open(HUMAN_SIM_STATE, encoding="utf-8-sig") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for key, value in state.items():
        if not str(key).startswith("poke_stat:name:"):
            continue
        name = str(value or "").strip()
        if not name or name in seen or len(name) > 16:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= limit:
            break
    return names


def _post(text: str, voice: str) -> tuple[int, int]:
    body = json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        HUB_TTS, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, len(resp.read())


def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    skip_prefix = "--no-prefix" in argv
    chat_only = "--chat-only" in argv
    names_arg = ""
    if "--names" in argv:
        idx = argv.index("--names")
        if idx + 1 < len(argv):
            names_arg = argv[idx + 1]
    args = [a for a in argv if not a.startswith("--") and a != names_arg]
    conf = _read_config()
    voice = args[0] if args else _configured_voice(conf)
    lines = [] if chat_only else _fixed_lines(conf)
    fixed_count = len(lines)
    actions = [] if chat_only else _action_lines()
    seen = set(lines)
    for line in actions:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    # 聊天固定文案（含「发来一张图片」）+ 昵称前缀版本
    chat = list(CHAT_FIXED_LINES)
    for line in chat:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    names: list[str] = []
    prefixed = 0
    if not skip_prefix:
        names = (
            [name.strip() for name in names_arg.split(",") if name.strip()]
            if names_arg
            else _known_names()
        )
        for name in names:
            for line in chat:
                item = f"{name}：{line}"
                if item not in seen:
                    seen.add(item)
                    lines.append(item)
                    prefixed += 1
    print(
        f"[prewarm] 声线={voice} 固定台词={fixed_count} 条 "
        f"动作台词={len(actions)} 条 聊天固定={len(chat)} 条 "
        f"昵称前缀={prefixed} 条（昵称 {len(names)} 个） 合计={len(lines)} 条"
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
