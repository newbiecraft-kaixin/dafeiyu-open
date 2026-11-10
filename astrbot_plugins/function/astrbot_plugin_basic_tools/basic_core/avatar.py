"""查头像 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import base64
import json
import os

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())

# 中枢「识图模型」：<data>/local_tools/vision.json，选了就优先用它看头像
_VISION_CFG_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "..", "local_tools", "vision.json",
    )
)
_vision_cfg_cache: tuple[float, str] = (0.0, "")

AVATAR_VISION_PROMPT = (
    "用两三句话描述这个QQ头像：主体是什么（动漫角色/真人/动物/"
    "文字/表情包/风景），画风和配色，有没有明显的梗或特征。"
    "只描述画面本身，不要客套，不要建议。"
)


def _central_vision_provider() -> str:
    """读中枢识图配置（带 mtime 缓存，中枢改完不用重启）。"""
    global _vision_cfg_cache
    try:
        mtime = os.path.getmtime(_VISION_CFG_PATH)
        if mtime != _vision_cfg_cache[0]:
            with open(_VISION_CFG_PATH, encoding="utf-8-sig") as f:
                pid = str((json.load(f) or {}).get("provider_id") or "").strip()
            _vision_cfg_cache = (mtime, pid)
        return _vision_cfg_cache[1]
    except Exception:  # noqa: BLE001
        return ""


class AvatarMixin:
    async def _fetch_bytes(self, url: str, limit: int = 5 * 1024 * 1024) -> bytes | None:
        """自己把图拉下来：交给 NapCat 下载 qlogo 时常超时，base64 直发更稳。"""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15), trust_env=False
            ) as sess:
                async with sess.get(url) as r:
                    if r.status != 200:
                        return None
                    # 必须用 read() 读完整响应：content.read(n) 只回缓冲区里已有的那段，
                    # 会把 JPEG 尾部截断，解码后下半张图会变成黑色/灰色。
                    data = await r.read()
                    if len(data) > limit:
                        logger.warning(f"[basic_tools] 头像过大 {len(data)} 字节，跳过")
                        return None
                    return data
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 头像下载失败: {type(e).__name__}: {str(e)[:60]}")
            return None

    async def _describe_avatar(self, url: str, body: bytes | None = None) -> str | None:
        """看头像：只走中枢「识图模型」，没开启就不识别（不连本地 ollama）。"""
        try:
            pid = _central_vision_provider()
            if not pid:
                return None
            if body is None:
                body = await self._fetch_bytes(url)
            if not body or len(body) < 500:
                return None
            b64 = base64.b64encode(body).decode("ascii")
            try:
                prov = self.context.get_provider_by_id(pid)
                if not prov:
                    return None
                resp = await prov.text_chat(
                    prompt=AVATAR_VISION_PROMPT,
                    image_urls=["base64://" + b64],
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                if text:
                    logger.info(f"[basic_tools] 头像识别(中枢): {text[:60]}")
                return text or None
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[basic_tools] 中枢识图失败: {type(e).__name__}: {str(e)[:80]}"
                )
                return None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[basic_tools] 头像识别失败: {type(e).__name__}: {str(e)[:80]}"
            )
            return None

    @filter.llm_tool(name="get_avatar")
    async def get_avatar(self, event: AstrMessageEvent, target: str = ""):
        """查看某个人的 QQ 头像：头像图会直接发到当前会话，并返回头像画面的描述和对方的群名片/身份。只要话题涉及头像就必须调用本工具，包括：用户说"看看TA头像/查一下某某的头像/这头像是什么/发我头像看看"，以及用户问"你能不能看到头像/你有查头像的功能吗/你能看到我的头像吗"这类能力询问——这类问题也必须调用本工具当场演示，绝对不要回答"看不到/没那功能/做不到"。点评保持你的损友风格，但头像的主人就在群里，别真把人骂破防。

        Args:
            target(string): 目标用户的QQ号或群名片/昵称（支持部分匹配）；留空表示当前说话的人
        """
        bot = getattr(event, "bot", None)
        gid = str(event.get_group_id() or "")
        target = (target or "").strip()
        sender_id = str(event.get_sender_id() or "")
        sender_label = (event.get_sender_name() or "").split("#")[0] or sender_id
        # 「我/主人」这类自称：本人说话就指他自己，别去群里按昵称瞎找
        self_calls = {"我", "本人", "自己", "我本人", "主人", "主子", "你主人"}
        owner = owner_qq()
        if not target or (target in self_calls and (not gid or sender_id == owner)):
            if target in self_calls and not gid and sender_id:
                uid = sender_id
            elif target in ("主人", "主子", "你主人") and not gid:
                uid = owner or sender_id
            else:
                uid = sender_id
            name_label = sender_label
        elif target.isdigit():
            uid = target
            name_label = f"QQ:{uid}"
        else:
            if not gid:
                # 私聊里除了自己没有别人，叫什么都当成本人
                uid = sender_id
                name_label = sender_label
            else:
                if bot is None:
                    return "当前环境拿不到 QQ 客户端"
                resolved = await self._resolve_member(bot, gid, target)
                if not resolved:
                    return f"在群里没找到叫「{target}」的人，可以换个叫法或先调 get_group_members 查名单"
                uid = resolved
                name_label = target
        if not uid:
            return "不知道要查谁的头像，给个 QQ 号或昵称"
        # 群资料：群名片/身份/等级
        profile = ""
        if gid and bot is not None:
            try:
                members = await bot.get_group_member_list(group_id=int(gid))
                for m in members:
                    if str(m.get("user_id")) == uid:
                        card = m.get("card") or m.get("nickname") or "-"
                        role = {"owner": "群主", "admin": "管理员"}.get(
                            m.get("role", ""), "成员"
                        )
                        lv = str(m.get("level") or "").strip()
                        profile = f"{card}(QQ:{uid})，{role}" + (
                            f"，群等级 lv{lv}" if lv else ""
                        )
                        if name_label.startswith("QQ:"):
                            name_label = card
                        break
            except Exception:  # noqa: BLE001
                pass
        # qlogo 有多个节点，挨个试，别一个超时就放弃
        url, blob = "", None
        for host in ("q1.qlogo.cn", "q4.qlogo.cn", "q2.qlogo.cn", "q3.qlogo.cn"):
            url = f"https://{host}/g?b=qq&nk={uid}&s=640"
            blob = await self._fetch_bytes(url)
            if blob:
                break
        # 先把图发出去，再做视觉识别：识别要几秒到几十秒，别让用户干等
        if blob:
            seg = {"type": "image",
                   "data": {"file": "base64://" + base64.b64encode(blob).decode()}}
        else:
            seg = {"type": "image", "data": {"file": url}}
        sent = True
        try:
            await self._push_segments(event, [seg])
        except Exception as e:  # noqa: BLE001
            sent = False
            logger.warning(f"[basic_tools] 头像图发送失败: {e}")
        desc = await self._describe_avatar(url, blob)
        if not sent and not desc:
            return (f"{name_label}（QQ:{uid}）的头像这次没拉下来（网络超时）。"
                    "跟用户说一句网络卡了、稍后再试或者去 QQ 资料页看，别说你没这个功能。")
        parts = [f"{name_label} 的头像已经发出来了（就是刚才那张图）。" if sent
                 else f"{name_label} 的头像图这次发送失败，但画面已经识别出来了，直接点评画面就行。"]
        if profile:
            parts.append(f"资料：{profile}。")
        parts.append(
            f"头像画面：{desc}"
            if desc
            else "头像画面没识别出来（识图失败，请在 中枢→模型→识图模型 设置识图模型），凭直觉点评就行。"
        )
        parts.append("现在结合头像内容说一句你的点评（损但不破防）。")
        return " ".join(parts)


exit_plugin_main(globals(), _REAL_MODULE_NAME)
