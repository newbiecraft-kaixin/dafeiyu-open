"""MCP 工具调用实现：桌宠气泡、语音合成、消息发送、长期记忆写入。"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import quote

from .config import DATA_DIR, VOICE_LABELS, VOICE_PROFILES
from .http_client import http_call as _http
from .probe import port_alive as _port_alive
from .registry import _online, _read_manifests, _service


def _call_pet(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    text = str(arguments.get("text") or "").strip()
    if not text:
        return False, "text 不能为空"
    state = str(arguments.get("state") or "idle")
    status, body = _http(
        "POST",
        "http://127.0.0.1:23334/state",
        {"source": "hub", "state": state, "message": text},
        timeout=12.0,
    )
    if status != 200:
        return False, body
    return True, {"text": text, "relayed": body}


def _call_pet_control(body: Dict[str, Any]) -> Tuple[bool, Any]:
    from .petctl import control

    try:
        return True, control(body)
    except (ValueError, RuntimeError, OSError) as error:
        return False, str(error)


def _pet_handler(action: str):
    """pet.* 统一转调 petctl.control，按 action 拼装请求体。"""
    def handle(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
        body: Dict[str, Any] = {"action": action}
        if action == "say":
            body["text"] = str(arguments.get("text") or "").strip()
            if arguments.get("duration") is not None:
                body["duration"] = arguments["duration"]
            if arguments.get("anim"):
                body["anim"] = str(arguments["anim"])
        elif action == "anim":
            body["name"] = str(arguments.get("name") or "").strip()
        elif action == "chat":
            body["text"] = str(arguments.get("text") or "").strip()
        elif action == "history":
            body["keep"] = arguments.get("keep", 10)
        return _call_pet_control(body)

    return handle


def _profile_prompt(folder: Path) -> str:
    """prompt.txt 里是备注（「无需参考文本」等）时返回空，让 TTS 走 cross_lingual。"""
    try:
        text = (folder / "prompt.txt").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    if (not text or text.startswith(("（", "("))
            or "无需参考文本" in text or "instruct" in text.lower()):
        return ""
    return text


def _call_voice(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    text = str(arguments.get("text") or "").strip()
    if not text:
        return False, "text 不能为空"
    voice = str(arguments.get("voice") or "himeko")
    profile = VOICE_PROFILES.get(voice)
    if profile is None:
        return False, f"未知音色 {voice}"
    prompt_text = _profile_prompt(profile)
    payload = {
        "text": text,
        "prompt_wav": str(profile / "ref.wav"),
        "prompt_text": prompt_text,
        "fmt": "wav",
        "speed": 1.0,
    }
    status, body = _http("POST", "http://127.0.0.1:8714/tts", payload, timeout=60.0)
    if status != 200 or not isinstance(body, (bytes, bytearray)):
        return False, body
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / f"tts-{int(time.time())}-{uuid.uuid4().hex[:6]}.wav"
    target.write_bytes(bytes(body))
    return True, {"path": str(target), "voice": voice, "label": VOICE_LABELS.get(voice, voice)}


def _maybe_open_window(arguments: Dict[str, Any], to: str, chat_type: str) -> None:
    """主动私聊成功 → 开启监听窗口，让原群 AI 继续盯着这个人的私聊。"""
    if str(chat_type or "").lower() != "private":
        return
    origin = str(arguments.get("origin_group") or "").strip()
    if not origin:
        return
    try:
        from .windows import start

        start(
            to,
            group_id=origin,
            group_umo=str(arguments.get("origin_umo") or ""),
            seconds=arguments.get("listen_seconds"),
        )
    except (OSError, ValueError, TypeError):
        pass


def _call_io(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    platform = str(arguments.get("platform") or "qq").lower()
    chat_type = str(arguments.get("chat_type") or "group").lower()
    to = str(arguments.get("to") or "").strip()
    text = str(arguments.get("text") or "").strip()
    if not to or not text:
        return False, "to / text 不能为空"

    if platform == "qq":
        # QQ 没有中间层：直接调 NapCat 的 OneBot HTTP，和 AstrBot 出站同一个协议端。
        if not _port_alive(3000):
            return False, "napcat 不在线"
        if chat_type not in {"group", "private"}:
            return False, "chat_type 只能是 group 或 private"
        action = "send_group_msg" if chat_type == "group" else "send_private_msg"
        field = "group_id" if chat_type == "group" else "user_id"
        try:
            target_id = int(to)
        except ValueError:
            return False, "to 必须是 QQ 群号或 QQ 号"
        from .botctl import _onebot_token

        token = _onebot_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        status, body = _http(
            "POST",
            f"http://127.0.0.1:3000/{action}",
            {field: target_id, "message": text},
            timeout=8.0,
            headers=headers,
        )
        if status != 200 or not isinstance(body, dict) or body.get("retcode") not in (0, None):
            return False, body
        _maybe_open_window(arguments, to, chat_type)
        return True, body

    manifest = _service(_read_manifests(), "io-wechat")
    if not manifest or not _online(manifest):
        return False, "io-wechat 不在线"
    status, body = _http(
        "POST",
        f"http://127.0.0.1:{manifest['port']}/send",
        {"to": to, "text": text, "chat_type": chat_type},
        # 微信是 UIA：切会话/等用户停手 + 发送通常要 10 秒以上，
        # 超时太短会触发 keepalive 重试，第二次被 io-wechat 当成「重复消息」拒掉，
        # 明明已经发出去却回报失败。给足 45 秒。
        timeout=45.0,
    )
    if status != 200:
        return False, body
    _maybe_open_window(arguments, to, chat_type)
    return True, body


def _call_memory(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    payload = {
        "scope": str(arguments.get("scope") or ""),
        "key": str(arguments.get("key") or ""),
        "value": str(arguments.get("value") or ""),
        "importance": arguments.get("importance", 1.0),
    }
    status, body = _http("POST", "http://127.0.0.1:23407/facts", payload, timeout=5.0)
    if status != 200:
        return False, body
    return True, body


def _call_video_look(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    """看视频：转给 video 服务的 /video/parse，拿标题/字幕/评论。"""
    url = str(arguments.get("url") or "").strip()
    if not url:
        return False, "url 不能为空"
    status, body = _http(
        "POST",
        "http://127.0.0.1:23368/video/parse",
        {
            "url": url,
            "with_comments": bool(arguments.get("with_comments", True)),
            "with_transcript": bool(arguments.get("with_transcript", False)),
        },
        timeout=120.0,
    )
    if status != 200:
        return False, body
    return True, body.get("data", body) if isinstance(body, dict) else body


def _call_video_feed(arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    """刷B站：转给 video 服务的 /video/feed，按标签取片单。"""
    try:
        count = max(1, min(20, int(arguments.get("count", 5))))
    except (TypeError, ValueError):
        count = 5
    tags = str(arguments.get("tags") or "").strip()
    target = f"http://127.0.0.1:23368/video/feed?count={count}"
    if tags:
        target += "&tags=" + quote(tags, safe=",")
    status, body = _http("GET", target, None, timeout=30.0)
    if status != 200:
        return False, body
    return True, body.get("data", body) if isinstance(body, dict) else body


HANDLERS = {
    "pet.speak": _call_pet,
    "pet.bubble": _pet_handler("say"),
    "pet.anim": _pet_handler("anim"),
    "pet.chat": _pet_handler("chat"),
    "pet.bubble.clear": _pet_handler("clear"),
    "pet.history.trim": _pet_handler("history"),
    "voice.say": _call_voice,
    "io.send": _call_io,
    "memory.remember": _call_memory,
    "video.look": _call_video_look,
    "video.feed": _call_video_feed,
}
