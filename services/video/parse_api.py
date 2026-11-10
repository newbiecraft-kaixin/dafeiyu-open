"""video.parse_api：看视频（B站 / 抖音 / 快手 / 西瓜 链接解析）。

属于「视频能力域」：贴一个链接，取标题、作者、时长、发布时间、简介、
字幕（没有字幕时可选择音频转写）和前 N 条评论。由 service.py 装配到 23368 端口。

接口
  POST /video/parse         解析链接
  GET  /video/config        读配置（Cookie 只回显是否已设置，不回显原值）
  POST /video/config        写配置
  GET  /video/parse/status  最近一次解析概况（页面顶部用）

实现按职责拆在同包子模块（本文件只保留 handlers 与转发，逻辑不变）：
  _parse_common      常量 / UA / 直连 opener / 页面与 JSON 小工具
  _parse_config      配置读写、最近解析概况、解析存档（字幕落盘）
  _parse_bili        B站解析
  _parse_media       抖音 / 快手 / 西瓜解析
  _parse_transcribe  音频下载、ffmpeg 抽轨、whisper / 本机字幕服务转写

所有第三方接口都 fail-soft：断网 / 超时 / 风控 / 结构变化只往 notes 里写中文说明，
或返回 fail(...)，绝不抛异常导致服务 500。视频解析参考了 AstrBot 插件
astrbot_plugin_web_lookup 的 video_core，但不依赖其运行时。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2] / "shared"))

from infra import fail, ok  # noqa: E402

from _parse_bili import _parse_bili  # noqa: E402
from _parse_common import (  # noqa: E402  转发常量与工具，保持 parse_api.X 可用
    COOKIE_KEYS,
    DATA_DIR,
    PLATFORM_NAMES,
    SUBTITLE_PORT,
    UA,
    UA_M,
    _as_bool,
    _detect_platform,
    _empty_result,
    _http,
    _payload,
)
from _parse_config import (  # noqa: E402  转发配置 / 状态 / 存档
    CONFIG_FILE,
    PARSED_DIR,
    _LAST,
    _LAST_LOCK,
    _list_parsed,
    _load_cfg,
    _normalize_cfg,
    _parsed_key,
    _public_cfg,
    _remember_last,
    _save_cfg,
    _save_parsed,
    status,
)
from _parse_media import _parse_douyin, _parse_ixigua, _parse_kuaishou  # noqa: E402


# ---------------- handlers ----------------

def handle_parse(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    url = str(body.get("url") or "").strip()
    if not url:
        return fail("请先贴一个视频链接", 400)
    want_comments = _as_bool(body.get("with_comments"), True)
    want_transcript = _as_bool(body.get("with_transcript"), False)

    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^BV[0-9A-Za-z]{10}$", url, re.I) or re.match(r"^av\d+$", url, re.I):
            url = "https://www.bilibili.com/video/" + url
        elif re.search(
            r"(bilibili\.com|b23\.tv|douyin\.com|kuaishou\.com|gifshow\.com|ixigua\.com|toutiao\.com)",
            url, re.I,
        ):
            url = "https://" + url.lstrip("/")

    platform = _detect_platform(url)
    if platform is None:
        return fail("暂不支持这个站点：目前支持 B站 / 抖音 / 快手 / 西瓜 的视频链接", 400)

    cfg = _load_cfg()
    deadline = time.time() + float(cfg["timeout_s"])
    started = time.time()
    parser = {
        "bilibili": _parse_bili,
        "douyin": _parse_douyin,
        "kuaishou": _parse_kuaishou,
        "ixigua": _parse_ixigua,
    }[platform]
    try:
        data = parser(url, cfg, want_comments, want_transcript, deadline)
    except Exception as exc:  # noqa: BLE001 - 兜底，不让服务 500
        data = _empty_result(platform, url, f"解析失败（{type(exc).__name__}），稍后再试或换个链接")
    data["url"] = url
    elapsed_ms = int((time.time() - started) * 1000)
    _remember_last(data, elapsed_ms)
    _save_parsed(data)
    return ok(data)


def handle_config_get(_req) -> Any:  # noqa: ANN001
    return ok(_public_cfg(_load_cfg()))


def handle_config_post(req) -> Any:  # noqa: ANN001
    body = _payload(req)
    cfg = _load_cfg()
    cookie = body.get("cookie")
    if isinstance(cookie, dict):
        for key in COOKIE_KEYS:
            if key in cookie:
                cfg["cookie"][key] = str(cookie.get(key) or "").strip()
    raw = dict(cfg)
    for key in ("comment_n", "transcript_chars", "audio_max_s", "timeout_s"):
        if key in body:
            raw[key] = body.get(key)
    cfg = _normalize_cfg(raw)
    try:
        _save_cfg(cfg)
    except OSError as exc:
        return fail(f"配置保存失败：{exc}", 500)
    payload = _public_cfg(cfg)
    payload["saved"] = True
    return ok(payload)


def handle_parse_status(_req) -> Any:  # noqa: ANN001
    with _LAST_LOCK:
        last = dict(_LAST) if _LAST else None
    return ok({"last": last, "platforms": list(PLATFORM_NAMES.values())})


def handle_history(_req) -> Any:  # noqa: ANN001
    return ok({"items": _list_parsed()})


def handle_parsed(req) -> Any:  # noqa: ANN001
    key = str(req.q("key") or "").strip()
    url = str(req.q("url") or "").strip()
    if not key and url:
        key = _parsed_key(url)
    if not re.fullmatch(r"[0-9a-f]{16}", key):
        return fail("缺少 key 或 url 参数", 400)
    path = PARSED_DIR / f"{key}.json"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fail("这条链接还没有解析存档", 404)
    return ok(rec)


routes: Dict[str, Any] = {
    "POST /video/parse": handle_parse,
    "GET /video/config": handle_config_get,
    "POST /video/config": handle_config_post,
    "GET /video/parse/status": handle_parse_status,
    "GET /video/history": handle_history,
    "GET /video/parsed": handle_parsed,
}
