"""QQ 空间功能层：看空间、留言、点赞、转发通知。"""

from __future__ import annotations

import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .qzone_config import DATA_ROOT
from .qzone_web import _strip_html


class QzoneActionsMixin:
    async def _auto_like(
        self, cookies: str, bkn: str, uin: str, target: str, feeds: list
    ) -> tuple[int, str]:
        """批量给自己的「看空间」结果点赞；失败就停手，别连着触发风控。"""
        liked = self.state.setdefault("liked", {})
        seen = set(liked.get(target) or [])
        ok_n, last_err = 0, ""
        for f in feeds:
            key = str(f.get("unikey") or f.get("fid") or f.get("tid") or "")
            if not key or key in seen:
                continue
            if f.get("isliked"):
                seen.add(key)
                continue
            ok, why = await self._like_feed(cookies, bkn, uin, f, True)
            if ok:
                ok_n += 1
                seen.add(key)
                continue
            last_err = why
            logger.warning(f"[qzone_channel] 点赞失败 {key[:40]}: {why}")
            break
        liked[target] = list(seen)[-200:]
        self._save_state()
        return ok_n, last_err

    async def _notify_owner(self, feed_text: str, who: str, text: str, draft: str) -> None:
        """回评发不出去时，把评论和拟好的回复私聊给主人（30 分钟最多一次，免得刷屏）。"""
        if not self.cfg.get("owner_notify", True):
            return
        now = time.time()
        if now - float(self.state.get("last_notify_ts") or 0) < 1800:
            return
        owner = str(self.cfg.get("owner_id") or "").strip() or self._owner_from_cmd_config()
        if not owner or not self._bot:
            return
        msg = (
            f"空间来评论了（回评接口被 token 拦住，先转给你）：\n"
            f"我发的说说：{feed_text[:80] or '（无文字）'}\n"
            f"{who}：{text[:80]}\n"
            f"我拟的回复：{draft}"
        )
        try:
            await self._bot.api.call_action(
                "send_private_msg", user_id=int(owner), message=msg
            )
            self.state["last_notify_ts"] = now
            self._save_state()
            logger.info("[qzone_channel] 已把空间评论和拟回复转给主人")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[qzone_channel] 转给主人失败: {type(e).__name__}: {e}")

    def _owner_from_cmd_config(self) -> str:
        try:
            path = os.path.join(DATA_ROOT, "cmd_config.json")
            with open(path, encoding="utf-8-sig") as f:
                admins = (json.load(f).get("admins_id") or [])
            return str(admins[0]) if admins else ""
        except Exception:  # noqa: BLE001
            return ""

    async def _friend_uins(self) -> set[str]:
        """机器人号的好友列表（缓存 15 分钟），用于「看空间」的前置拦截。"""
        cache = self.state.get("friends") or {}
        if (
            isinstance(cache.get("uins"), list)
            and time.time() - float(cache.get("ts") or 0) < 900
        ):
            return {str(x) for x in cache["uins"]}
        uins: set[str] = set()
        try:
            resp = await self._bot.api.call_action("get_friend_list")
            for item in resp or []:
                if isinstance(item, dict):
                    u = item.get("user_id") or item.get("uin")
                    if u:
                        uins.add(str(u))
        except Exception as e:  # noqa: BLE001
            # 拿不到好友列表就当非好友，让「公开动态」校验做最后一道
            logger.debug(f"[qzone_channel] 取好友列表失败: {type(e).__name__}: {e}")
            return set()
        self.state["friends"] = {"uins": list(uins), "ts": time.time()}
        self._save_state()
        return uins

    async def _view_gate(self, target: str, my_uin: str, requester: str = "") -> str:
        """「看空间」的前置拦截：主人和好友的空间不扒（避免泄露私密）。

        机器人自己的空间允许只读查看；主人本人看自己的空间放行；
        非好友的公开动态照常放行。
        """
        owner = self._owner_from_cmd_config()
        if str(target) == str(my_uin):
            return ""
        if owner and str(target) == owner:
            if requester and str(requester) == owner:
                return ""
            return "这是主人的空间，按隐私规矩不扒；想看直接去 QQ 空间翻"
        if not self._ensure_bot():
            return "还没连上 QQ 客户端，稍后再试"
        if str(target) in await self._friend_uins():
            return (
                f"{target} 是好友，按现在的规矩只扒非好友的公开空间，"
                "好友的空间涉及隐私就不看了～"
            )
        return ""

    def _parse_view_args(
        self, event: AstrMessageEvent, target: str = "", count: int = 0
    ) -> tuple[str, int]:
        """解析 /看空间 的目标和条数：QQ 号从 @/参数取，条数从剩余的纯数字取。"""
        default = max(1, int(self.cfg.get("view_default_count", 5) or 5))
        cap = max(default, int(self.cfg.get("view_max_count", 20) or 20))
        n = int(count or 0)
        if n <= 0:
            # 命令参数里只剩一个小数字就是条数；QQ 号（≥5 位）不会被误当成条数
            for x in re.findall(r"\d+", str(target or "")):
                if len(x) <= 3 and int(x) > 0:
                    n = int(x)
                    break
            if n <= 0:
                text = re.sub(r"^[//／]+看空间", "", str(getattr(event, "message_str", "") or ""))
                # @昵称(QQ号) 里的数字先摘掉，免得把昵称里的数字当条数
                text = re.sub(r"@[^\s]*?\(\d+\)", "", text)
                for x in re.findall(r"\d+", text):
                    if len(x) <= 3 and int(x) > 0:
                        n = int(x)
                        break
        n = n if n > 0 else default
        t = self._resolve_qzone_target(event, target)
        if not t:
            # @ 没解析出来时，退化成消息里第一个 ≥5 位的数字
            for x in re.findall(r"\d{5,12}", str(getattr(event, "message_str", "") or "")):
                t = x
                break
        return t, min(n, cap)

    def _resolve_qzone_target(self, event: AstrMessageEvent, target: str = "") -> str:
        """从参数或 @ 段里解析目标 QQ 号。"""
        m = re.search(r"(\d{5,12})", str(target or ""))
        if m:
            return m.group(1)
        for seg in (getattr(getattr(event, "message_obj", None), "message", None) or []):
            qq = getattr(seg, "qq", None)
            if qq and str(qq).isdigit():
                return str(qq)
        return ""

    def _parse_message_args(
        self, event: AstrMessageEvent, target: str = "", content: str = ""
    ) -> tuple[str, str]:
        """解析 /留言 的目标和内容：@昵称(QQ号) 和命令词先摘掉，剩下的就是留言正文。"""
        t = self._resolve_qzone_target(event, target)
        if content:
            return t, re.sub(r"\s+", " ", str(content)).strip()
        raw = str(getattr(event, "message_str", "") or "")
        raw = re.sub(r"^[//／]*留言", "", raw).strip()
        raw = re.sub(r"@[^\s]*?\(\d+\)", "", raw)  # 摘掉 @昵称(QQ号)
        if t:
            raw = raw.replace(t, "", 1)            # 显式 QQ 号也摘掉
        raw = raw.strip(" \u3000，,：:、")
        return t, re.sub(r"\s+", " ", raw).strip()

    async def leave_message(self, target: str, content: str) -> str:
        content = _strip_html(content)[:120].strip()
        if not content:
            return "留言内容不能为空"
        if not self._ensure_bot():
            return "还没连上 QQ 客户端，稍后再试"
        try:
            cookies, bkn, my_uin = await self._get_auth()
        except Exception as e:  # noqa: BLE001
            return f"拿不到空间登录凭据：{type(e).__name__}"
        try:
            feeds = await self._fetch_other_feeds(cookies, bkn, my_uin, target, 1)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qzone_channel] 留言取说说失败 uin={target}: {type(e).__name__}: {e}")
            return f"没拉到 {target} 的空间动态（{type(e).__name__}）"
        if not feeds:
            return f"{target} 的空间里没看到公开动态（可能设了隐私限制）"
        f = feeds[0]
        tid = str(f.get("tid") or f.get("fid") or "")
        if not tid:
            return "没找到这条说说的 id，留言发不出去"
        ok = await self._post_comment(cookies, bkn, my_uin, tid, content, host_uin=target)
        if ok:
            logger.info(f"[qzone_channel] 已留言 {target}: {content[:40]}")
            return f"给 {target} 的说说留言成功：{content}"
        if self._post_blocked:
            return "留言没发出去：空间写接口要 qzonetoken，机器人号这次没拿到（读说说、点赞不影响）"
        code = self._last_post_code
        return f"留言失败{'（code=' + str(code) + '）' if code is not None else ''}，可能被风控或接口变了，稍后再试"

    def _parse_like_args(
        self, event: AstrMessageEvent, target: str = "", index: int = 0
    ) -> tuple[str, int]:
        """解析 /点赞 的目标和序号：QQ 号从 @/参数取，序号取剩余的 1~2 位数字。"""
        t = self._resolve_qzone_target(event, target)
        if not t:
            for x in re.findall(r"\d{5,12}", str(getattr(event, "message_str", "") or "")):
                t = x
                break
        idx = int(index or 0)
        if idx <= 0:
            raw = re.sub(r"^[//／]*点赞", "", str(getattr(event, "message_str", "") or ""))
            raw = re.sub(r"@[^\s]*?\(\d+\)", "", raw)
            if t:
                raw = raw.replace(t, "", 1)
            for x in re.findall(r"\d+", raw):
                if len(x) <= 2:
                    continue
                if int(x) > 0:
                    idx = int(x)
                    break
        cap = max(1, int(self.cfg.get("view_max_count", 20) or 20))
        return t, max(1, min(idx or 1, cap))

    async def like_qzone_feed(self, target: str, index: int = 1) -> str:
        """给 target 的第 index 条（1=最近）空间说说点赞，返回一句人话结果。"""
        if not self._ensure_bot():
            return "还没连上 QQ 客户端，稍后再试"
        try:
            cookies, bkn, my_uin = await self._get_auth()
        except Exception as e:
            return f"拿不到空间登录凭据：{type(e).__name__}"
        if str(target) == str(my_uin):
            return "自己的说说点不了赞（空间不允许给自己点赞）"
        try:
            feeds = await self._fetch_other_feeds(
                cookies, bkn, my_uin, target, max(1, int(index))
            )
        except Exception as e:
            logger.warning(
                f"[qzone_channel] 点赞取说说失败 uin={target}: {type(e).__name__}: {e}"
            )
            return f"没拉到 {target} 的空间动态（{type(e).__name__}）"
        if not feeds:
            return f"{target} 的空间里没看到公开动态（可能设了隐私限制）"
        if index > len(feeds):
            return f"{target} 最近只有 {len(feeds)} 条公开动态，没有第 {index} 条"
        f = feeds[index - 1]
        if f.get("isliked"):
            return f"{target} 的第 {index} 条说说已经赞过了"
        ok, why = await self._like_feed(cookies, bkn, my_uin, f, True)
        if not ok:
            logger.warning(f"[qzone_channel] 点赞失败 uin={target} idx={index}: {why}")
            return f"点赞没成功：{why}"
        logger.info(f"[qzone_channel] 已点赞 {target} 的第 {index} 条说说")
        return f"给 {target} 的第 {index} 条说说点了个赞"

    async def view_qzone_feeds(self, target: str, limit: int = 0, requester: str = "") -> str:
        cap = max(1, int(self.cfg.get("view_max_count", 20) or 20))
        limit = max(1, min(int(limit or 0) or int(self.cfg.get("view_default_count", 5) or 5), cap))
        if not self._ensure_bot():
            return "还没连上 QQ 客户端，稍后再试"
        try:
            cookies, bkn, my_uin = await self._get_auth()
        except Exception as e:  # noqa: BLE001
            return f"拿不到空间登录凭据：{type(e).__name__}"
        target = str(target or my_uin)  # 没指定就看自己的空间
        blocked = await self._view_gate(target, my_uin, requester)
        if blocked:
            return blocked
        try:
            feeds = await self._fetch_other_feeds(cookies, bkn, my_uin, target, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qzone_channel] 看空间失败 uin={target}: {type(e).__name__}: {e}")
            return f"{target} 的空间没扒到（对方可能设了仅好友可见/私密，或接口限流）"
        if not feeds:
            return f"{target} 的空间里没看到公开动态（对方设了仅好友可见或私密就不看了）"
        lines = []
        for i, f in enumerate(feeds[:limit], 1):
            txt = _strip_html(f.get("content") or "") or "（图片/转发）"
            extra = []
            if f.get("pics"):
                extra.append(f"图{len(f.get('pics') or [])}")
            if f.get("comments"):
                extra.append(f"评{len(f.get('comments') or [])}")
            if f.get("likecount"):
                extra.append(f"赞{f['likecount']}")
            tag = f"（{'/'.join(extra)}）" if extra else ""
            when = str(f.get("time") or "").strip()
            prefix = f"{when} " if when else ""
            lines.append(f"{i}. {prefix}{txt[:100]}{tag}")
        tail = ""
        if self.cfg.get("view_auto_like", True) and str(target) != str(my_uin):
            n, err = await self._auto_like(cookies, bkn, my_uin, target, feeds[:limit])
            if n:
                tail = f"\n（顺手点赞 {n} 条）"
            elif err:
                tail = f"\n（点赞没成功：{err}）"
        return f"{target} 最近 {len(lines)} 条空间动态：\n" + "\n".join(lines) + tail

    async def view_like_comment(self, target: str, limit: int = 0) -> str:
        """看空间：逐条点赞 + 评论。已经赞过/评过的不再重复，失败就停手。"""
        cap = max(1, int(self.cfg.get("view_max_count", 20) or 20))
        limit = max(
            1,
            min(
                int(limit or 0) or int(self.cfg.get("view_default_count", 5) or 5),
                cap,
            ),
        )
        if not self._ensure_bot():
            return "还没连上 QQ 客户端，稍后再试"
        try:
            cookies, bkn, my_uin = await self._get_auth()
        except Exception as e:  # noqa: BLE001
            return f"拿不到空间登录凭据：{type(e).__name__}"
        blocked = await self._view_gate(target, my_uin)
        if blocked:
            return blocked
        try:
            feeds = await self._fetch_other_feeds(cookies, bkn, my_uin, target, limit)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[qzone_channel] 看空间失败 uin={target}: {type(e).__name__}: {e}"
            )
            return f"{target} 的空间没扒到（对方可能设了仅好友可见/私密，或接口限流）"
        if not feeds:
            return f"{target} 的空间里没看到公开动态（对方设了仅好友可见或私密就不看了）"

        liked = self.state.setdefault("liked", {})
        seen_like = set(liked.get(target) or [])
        commented = self.state.setdefault("commented", {})
        seen_cmt = set(commented.get(target) or [])
        lines: list[str] = []
        like_err = ""
        cmt_err = ""
        for i, f in enumerate(feeds[:limit], 1):
            key = str(f.get("unikey") or f.get("fid") or f.get("tid") or "")
            tid = str(f.get("tid") or f.get("fid") or "")
            txt = _strip_html(f.get("content") or "") or "（图片/转发）"
            marks: list[str] = []

            # 点赞：页面已标已赞，或本地记过，都跳过
            if f.get("isliked") or (key and key in seen_like):
                if key:
                    seen_like.add(key)
                marks.append("已赞")
            elif like_err:
                marks.append("赞跳过")
            else:
                ok, why = await self._like_feed(cookies, bkn, my_uin, f, True)
                if ok:
                    if key:
                        seen_like.add(key)
                    marks.append("赞")
                else:
                    like_err = why
                    marks.append("赞失败")

            # 评论：同一条说说只评一次；生成不出内容就跳过
            if key and key in seen_cmt:
                marks.append("已评")
            elif not tid:
                marks.append("评跳过(无id)")
            elif cmt_err:
                marks.append("评跳过")
            else:
                draft = await self._compose_feed_comment(txt)
                if not draft:
                    cmt_err = "评论内容生成失败"
                    marks.append("评失败")
                else:
                    ok = await self._post_comment(
                        cookies, bkn, my_uin, tid, draft, host_uin=target
                    )
                    if ok:
                        if key:
                            seen_cmt.add(key)
                        marks.append(f"评「{draft}」")
                    else:
                        cmt_err = (
                            "评论接口要 qzonetoken"
                            if self._post_blocked
                            else "评论失败"
                        )
                        marks.append("评失败")

            lines.append(f"{i}. {txt[:60]}（{'/'.join(marks)}）")

        liked[target] = list(seen_like)[-200:]
        commented[target] = list(seen_cmt)[-200:]
        self._save_state()

        head = f"{target} 最近 {len(lines)} 条空间动态（逐条点赞+评论）：\n" + "\n".join(
            lines
        )
        if like_err or cmt_err:
            head += "\n（" + "；".join(
                x for x in (f"点赞中断：{like_err}" if like_err else "",
                            f"评论中断：{cmt_err}" if cmt_err else "") if x
            ) + "）"
        return head
