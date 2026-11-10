"""会话指针的本地持久化。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict


class SessionStore:
    """key（角色+平台+会话+发送者）-> DSH session id。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = {}
        if isinstance(raw, dict):
            self._data = {
                str(k): v for k, v in raw.items() if isinstance(v, dict) and v.get("session_id")
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str) -> str:
        with self._lock:
            return str(self._data.get(key, {}).get("session_id") or "")

    def set(self, key: str, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._data[key] = {"session_id": session_id, "updated": time.time()}
            self._save()

    def drop(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._save()

    def count(self) -> int:
        with self._lock:
            return len(self._data)
