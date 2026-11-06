"""最近调用记录（进程内，最多保留 50 条）。"""
from __future__ import annotations

import time

_RECORDS: list[dict] = []


def remember(kind: str, src: str, text: str, cost: float) -> None:
    _RECORDS.append(
        {
            "kind": kind,
            "src": src[:120],
            "text": (text or "")[:300],
            "chars": len(text or ""),
            "seconds": round(cost, 2),
            "at": int(time.time()),
        }
    )
    del _RECORDS[:-50]


def snapshot() -> list[dict]:
    return list(reversed(_RECORDS))


def count() -> int:
    return len(_RECORDS)
