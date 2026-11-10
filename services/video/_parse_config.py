"""video.parse_api 的持久化状态：配置读写、最近一次解析概况与解析存档（字幕落盘）。

从 parse_api.py 按职责拆出（纯移动，逻辑与签名不变）；
配置锁 / 最近解析锁 / 存档锁等全局可变状态全部归属本模块。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List

from _parse_common import COOKIE_KEYS, DATA_DIR, PLATFORM_NAMES

CONFIG_FILE = DATA_DIR / "parse.json"

_CFG_LOCK = threading.RLock()
_LAST_LOCK = threading.Lock()
_LAST: Dict[str, Any] = {}


# ---------------- 配置读写 ----------------

def _normalize_cfg(raw: Any) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cookie_raw = raw.get("cookie")
    cookie = {key: "" for key in COOKIE_KEYS}
    if isinstance(cookie_raw, dict):
        for key in COOKIE_KEYS:
            cookie[key] = str(cookie_raw.get(key) or "").strip()
    cfg: Dict[str, Any] = {"cookie": cookie}
    bounds = (
        ("comment_n", 1, 50, 10),
        ("transcript_chars", 500, 20000, 4000),
        ("audio_max_s", 30, 1800, 300),
        ("timeout_s", 10, 600, 180),
    )
    for key, low, high, default in bounds:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        cfg[key] = max(low, min(high, value))
    return cfg


def _load_cfg() -> Dict[str, Any]:
    with _CFG_LOCK:
        raw: Any = {}
        try:
            if CONFIG_FILE.exists():
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raw = {}
        return _normalize_cfg(raw)


def _save_cfg(cfg: Dict[str, Any]) -> None:
    with _CFG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)


def _public_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """对外只给「是否已设置」，绝不回显 Cookie 原值。"""
    return {
        "cookie": {key: bool(cfg["cookie"].get(key)) for key in COOKIE_KEYS},
        "comment_n": cfg["comment_n"],
        "transcript_chars": cfg["transcript_chars"],
        "audio_max_s": cfg["audio_max_s"],
        "timeout_s": cfg["timeout_s"],
    }


def status() -> Dict[str, Any]:
    """给 /health 用：模块就绪、平台列表、Cookie 是否已设置、最近一次解析概况。"""
    try:
        cfg = _load_cfg()
    except Exception:  # noqa: BLE001 - health 不允许把服务带崩
        cfg = _normalize_cfg({})
    with _LAST_LOCK:
        last = dict(_LAST) if _LAST else None
    return {
        "ready": True,
        "platforms": list(PLATFORM_NAMES.values()),
        "cookies": {key: bool(cfg["cookie"].get(key)) for key in COOKIE_KEYS},
        "transcript": "faster-whisper/small（本地 CPU）",
        "last": last,
    }


def _remember_last(data: Dict[str, Any], elapsed_ms: int) -> None:
    with _LAST_LOCK:
        _LAST.clear()
        _LAST.update({
            "at": time.strftime("%H:%M:%S"),
            "platform": data.get("platform") or "",
            "title": data.get("title") or "",
            "url": data.get("url") or "",
            "has_subtitle": bool(data.get("subtitle")),
            "comments": len(data.get("comments") or []),
            "notes": len(data.get("notes") or []),
            "elapsed_ms": elapsed_ms,
        })


# ---------------- 解析存档（字幕全文落盘） ----------------

PARSED_DIR = DATA_DIR / "parsed"
_HIST_LOCK = threading.Lock()


def _parsed_key(url: str) -> str:
    return hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:16]


def _save_parsed(data: Dict[str, Any]) -> None:
    """每次解析成功后把完整结果（含字幕/转写全文）落盘，同链接重复解析覆盖；
    新结果没拿到字幕时保留旧存档的字幕，避免一次失败把已有内容清空。"""
    if not (data.get("title") or data.get("subtitle")):
        return
    key = _parsed_key(data.get("url") or "")
    path = PARSED_DIR / f"{key}.json"
    record = {
        "key": key,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": data.get("url") or "",
        "platform": data.get("platform") or "",
        "title": data.get("title") or "",
        "author": data.get("author") or "",
        "duration": data.get("duration") or 0,
        "publish_time": data.get("publish_time") or "",
        "desc": data.get("desc") or "",
        "subtitle": data.get("subtitle") or "",
        "subtitle_source": data.get("subtitle_source") or "",
        "comments": data.get("comments") or [],
        "notes": data.get("notes") or [],
    }
    with _HIST_LOCK:
        try:
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
                if not record["subtitle"] and old.get("subtitle"):
                    record["subtitle"] = str(old["subtitle"])
                    record["subtitle_source"] = str(old.get("subtitle_source") or "字幕")
            PARSED_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, ValueError):
            pass


def _list_parsed(limit: int = 200) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        files = sorted(PARSED_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return rows
    for p in files[:limit]:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        rows.append({
            "key": rec.get("key") or p.stem,
            "saved_at": rec.get("saved_at") or "",
            "url": rec.get("url") or "",
            "platform": rec.get("platform") or "",
            "title": rec.get("title") or "",
            "author": rec.get("author") or "",
            "duration": rec.get("duration") or 0,
            "sub_chars": len(str(rec.get("subtitle") or "")),
            "subtitle_source": rec.get("subtitle_source") or "",
        })
    return rows
