"""主脑执行入口：profile / 人设 / 会话留在本地，模型调度交给 model 层。"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from infra import keepalive
from roles import Role, RolesConfig

from .errors import HarnessError
from .profile import (
    PROFILE_CORDIS,
    PROFILE_PACKAGE,
    load_persona,
    render_patch,
)
from .result import ChatResult
from .sessions import SessionStore

LOG = logging.getLogger("brain.harness")


class Harness:
    """主脑执行入口：profile / 人设 / 会话留在本地，模型调度交给 model 层。"""

    def __init__(
        self,
        config: RolesConfig,
        *,
        sessions_file: str | Path | None = None,
        logger: Optional[logging.Logger] = None,
        model_url: str | None = None,
    ) -> None:
        self.config = config
        self.log = logger or LOG
        self.sessions = SessionStore(
            sessions_file or config.project_root / "services" / "brain" / "data" / "sessions.json"
        )
        self.timeout_s = float(config.harness_setting("timeout_s", 180) or 180)
        self.max_parallel = max(1, int(config.harness_setting("max_parallel", 3) or 3))
        self._sem = threading.BoundedSemaphore(self.max_parallel)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._profiles: Dict[str, Path] = {}
        self._profiles_guard = threading.Lock()
        self._persona_cache: Dict[str, str] = {}
        self.model_url = (
            str(model_url or os.environ.get("MODEL_URL") or "").strip()
            or f"http://127.0.0.1:{int(os.environ.get('MODEL_PORT') or 23406)}"
        ).rstrip("/")

    # ------------------------------------------------------------ profile
    def _profile_dir(self, role: Role) -> Path:
        return self.config.dsh_home() / "profiles" / role.profile

    def _persona(self, role: Role) -> str:
        cached = self._persona_cache.get(role.name)
        if cached is None:
            cached = load_persona(role.persona_files)
            self._persona_cache[role.name] = cached
        return cached

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> None:
        if path.is_file():
            try:
                if path.read_text(encoding="utf-8-sig") == content:
                    return
            except OSError:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def ensure_profile(self, role: Role) -> Path:
        with self._profiles_guard:
            known = self._profiles.get(role.profile)
        if known:
            return known

        directory = self._profile_dir(role)
        self._write_if_changed(directory / "package.json", PROFILE_PACKAGE.format(profile=role.profile))
        self._write_if_changed(directory / "cordis.yml", PROFILE_CORDIS)
        self._write_if_changed(directory / "cordis.patch.yml", render_patch(role, self._persona(role)))
        role.workspace.mkdir(parents=True, exist_ok=True)

        with self._profiles_guard:
            self._profiles[role.profile] = directory
        self.log.info("harness profile ready: %s -> %s", role.profile, directory)
        return directory

    # ------------------------------------------------------------ 调用 model 层
    def _conversation_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            # 与 model 层复用同一条长连接，消息不必每条都重新建连
            data = keepalive.request_json(
                "POST", self.model_url + path, payload, timeout=self.timeout_s
            )
        except keepalive.KeepAliveError as exc:
            detail = exc.body.decode("utf-8", "replace")[:300]
            raise HarnessError(f"model 层 HTTP {exc.status}: {detail}") from exc
        except (OSError, TimeoutError, ValueError) as exc:
            raise HarnessError(f"连不上 model 层（{self.model_url}）: {exc}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            detail = data.get("error") if isinstance(data, dict) else data
            raise HarnessError(str(detail or "model 层返回异常"))
        return data.get("data") or {}

    def chat(
        self,
        role: Role,
        key: str,
        text: str,
        *,
        session_id: str = "",
        scene: str = "",
        context: str = "",
    ) -> ChatResult:
        """按会话串行调用一次 model 层；首次自动新建会话，之后用 session_id 续聊。"""
        text = str(text or "").strip()
        if not text:
            raise HarnessError("消息文本为空")

        self.ensure_profile(role)
        sid = session_id or self.sessions.get(key)
        content = f"{context.strip()}\n{text}" if context.strip() else text
        started = time.perf_counter()

        with self._conversation_lock(key):
            with self._sem:
                result = self._post(
                    "/complete",
                    {
                        "role": role.name,
                        "profile": role.profile,
                        "model": role.model,
                        "session_id": sid,
                        "scene": scene,
                        "messages": [{"role": "user", "content": content}],
                    },
                )

        new_sid = str(result.get("session_id") or "")
        if new_sid:
            self.sessions.set(key, new_sid)
        reply = str(result.get("reply") or "").strip()
        if not reply:
            raise HarnessError("model 层返回了空回复")

        return ChatResult(
            reply=reply,
            session_id=new_sid or sid,
            latency_ms=int((time.perf_counter() - started) * 1000),
            role=role.name,
            profile=role.profile,
            provider=str(result.get("provider") or ""),
            model=str(result.get("model") or ""),
        )

    # ------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        return {
            "dsh_root": str(self.config.dsh_root()),
            "dsh_home": str(self.config.dsh_home()),
            "model_url": self.model_url,
            "timeout_s": self.timeout_s,
            "max_parallel": self.max_parallel,
            "sessions": self.sessions.count(),
            "profiles": {
                r.name: r.profile for r in self.config.roles.values()
            },
        }


def build_key(role: Role, platform: str, chat_id: str, chat_type: str, sender_id: str) -> str:
    """一个会话一把锁、一个 session id：群聊按群，私聊按人。"""
    if chat_type == "private":
        return f"{role.name}|{platform}|private|{sender_id or chat_id}"
    return f"{role.name}|{platform}|{chat_type}|{chat_id}"
