"""MCP 工具目录：纯数据，描述对外暴露的工具与入参。"""
from __future__ import annotations

from typing import Any, Dict, List

from .config import VOICE_LABELS

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "pet.speak",
        "description": "让桌宠显示一句气泡（语音缺失时自动只显示字幕）",
        "requires": "pet",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要显示的短句"},
                "state": {"type": "string", "description": "idle/working/success/error 等"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "pet.bubble",
        "description": "直接让桌宠显示一句气泡（不走旁白生成，可指定停留时长与动作）",
        "requires": "pet",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要显示的短句"},
                "duration": {"type": "number", "description": "气泡停留毫秒数"},
                "anim": {"type": "string", "description": "同时播放的动作名"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "pet.anim",
        "description": "让桌宠播放一个动作",
        "requires": "pet",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "动作名"}},
            "required": ["name"],
        },
    },
    {
        "name": "pet.chat",
        "description": "和桌宠对话，返回它的回复",
        "requires": "pet",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "pet.bubble.clear",
        "description": "清空桌宠当前气泡",
        "requires": "pet",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pet.history.trim",
        "description": "清理桌宠旧对话，只保留最近 keep 轮（1 轮 = 1 问 1 答）",
        "requires": "pet",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keep": {"type": "number", "description": "保留轮数，默认 10；0 = 全部清空"},
            },
        },
    },
    {
        "name": "voice.say",
        "description": "用指定音色合成语音，返回 wav 路径（不可用时该工具不出现）",
        "requires": "voice",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "voice": {"type": "string", "enum": list(VOICE_LABELS)},
            },
            "required": ["text"],
        },
    },
    {
        "name": "io.send",
        "description": "通过 QQ 或微信发送一条消息（任一平台在线即可）",
        "optional_backends": ["napcat", "io-wechat"],
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["qq", "wechat"]},
                "to": {"type": "string"},
                "text": {"type": "string"},
                "chat_type": {"type": "string", "enum": ["group", "private"]},
                "origin_group": {"type": "string", "description": "主动私聊时原群群号；发送成功后开启监听窗口"},
                "origin_umo": {"type": "string", "description": "原群 UMO，可选"},
                "listen_seconds": {"type": "number", "description": "监听窗口时长，默认 300 秒"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "memory.remember",
        "description": "写入一条长期记忆",
        "requires": "store",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "key": {"type": "string"},
                "value": {"type": "string"},
                "importance": {"type": "number"},
            },
            "required": ["scope", "key", "value"],
        },
    },
    {
        "name": "video.look",
        "description": "看一个视频：给链接，返回标题、作者、时长、字幕和热门评论（支持 B站/抖音/快手/西瓜）",
        "requires": "video",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "视频链接，或 B站 BV/av 号"},
                "with_comments": {"type": "boolean", "description": "是否取热门评论，默认 true"},
                "with_transcript": {"type": "boolean", "description": "没有字幕时是否转写音频，默认 false"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "video.feed",
        "description": "刷一批 B站：按标签取几条能看的视频，返回标题、UP主、时长和链接",
        "requires": "video",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tags": {"type": "string", "description": "标签，逗号分隔；留空用已配置的标签"},
                "count": {"type": "number", "description": "条数，默认 5"},
            },
        },
    },
]
