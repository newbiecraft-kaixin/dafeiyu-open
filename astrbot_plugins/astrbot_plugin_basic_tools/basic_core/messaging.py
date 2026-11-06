"""跨会话发送与会话补写 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import base64
import os
import re
import time


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image, Plain, Record

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())

_VOICE_TRUE = {"1", "true", "yes", "on", "y", "语音", "voice"}


def _voice_flag(value: str) -> bool:
    """工具参数里 as_voice 可能被模型写成 true / 语音 / 1，统一判一下。"""
    return str(value or "").strip().lower() in _VOICE_TRUE


class MessagingMixin:
    async def _resolve_member(self, bot, gid: str, keyword: str) -> str | None:
        """在群里按群名片/昵称关键词找人，返回 QQ 号"""
        try:
            members = await bot.get_group_member_list(group_id=int(gid))
        except Exception:
            return None
        keyword = keyword.strip()
        for m in members:
            card = (m.get("card") or "").strip()
            nick = (m.get("nickname") or "").strip()
            if keyword and (keyword == card or keyword == nick or keyword in card or keyword in nick):
                return str(m.get("user_id"))
        return None

    async def _find_member_anywhere(self, bot, keyword: str) -> tuple[str, str] | None:
        """在机器人所在的群里挨个按昵称/群名片找人，返回 (QQ号, 群号)。

        私聊里只知道昵称、好友列表里又没有时用这个；找到群号后就能走群临时会话。
        """
        try:
            groups = await bot.get_group_list()
        except Exception:  # noqa: BLE001
            return None
        gids = [str(g.get("group_id") or "") for g in groups]
        for gid in [g for g in gids if g][:20]:
            uid = await self._resolve_member(bot, gid, keyword)
            if uid:
                return uid, gid
        return None

    async def _friend_ids(self, bot) -> set[str]:
        """好友 QQ 号集合（5 分钟缓存，好友列表很小但没必要每次发送都拉）"""
        ts, ids = self._friend_cache
        if ids and time.time() - ts < 300:
            return ids
        try:
            friends = await bot.get_friend_list()
        except Exception:  # noqa: BLE001
            return ids
        ids = {str(f.get("user_id")) for f in friends if f.get("user_id")}
        self._friend_cache = (time.time(), ids)
        return ids

    async def _is_friend(self, bot, uid: str) -> bool:
        return str(uid) in await self._friend_ids(bot)

    async def _resolve_friend(self, bot, keyword: str) -> str:
        """按备注/昵称在好友里找人"""
        try:
            friends = await bot.get_friend_list()
        except Exception:  # noqa: BLE001
            return ""
        kw = keyword.strip()
        for f in friends:
            name = str(f.get("remark") or "").strip()
            nick = str(f.get("nickname") or "").strip()
            if kw and (
                kw == name
                or kw == nick
                or (name and kw in name)
                or (nick and kw in nick)
            ):
                return str(f.get("user_id") or "")
        return ""

    async def _find_shared_group(self, bot, uid: str) -> str:
        """找和对方共有的群，人数少的优先（临时会话成功率更高）"""
        try:
            groups = await bot.get_group_list()
        except Exception:  # noqa: BLE001
            return ""
        cand = sorted(groups, key=lambda g: int(g.get("member_count") or 999999))
        for g in cand[:8]:
            gid = str(g.get("group_id") or "")
            if not gid.isdigit():
                continue
            try:
                resp = await bot.api.call_action(
                    "get_group_member_info",
                    **{"group_id": int(gid), "user_id": int(uid)},
                )
            except Exception:  # noqa: BLE001
                continue
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict) and str(data.get("user_id") or "") == str(uid):
                return gid
        return ""

    async def _voice_segments(self, text: str) -> list[dict]:
        """把一段文字合成语音消息段（跨群/跨会话发送用）。

        和 send_voice 同一条链路：CosyVoice2 克隆声线优先，服务挂了回退 edge-tts。
        返回空列表 = 没合成出可用音频。
        """
        import edge_tts

        text = (text or "").strip()[:200]
        if not text:
            return []
        ve = _load_voice_engine()
        os.makedirs(VOICE_DIR, exist_ok=True)
        segs: list[dict] = []
        for i, part in enumerate(_split_voice(text)):
            out = os.path.join(
                VOICE_DIR, f"voice_{int(time.time() * 1000)}_{i}.mp3"
            )
            if ve is not None:
                await ve.synth(part, out, voice=ve.EDGE_VOICE, mood="乖巧")
            else:
                await edge_tts.Communicate(
                    part, "zh-CN-XiaoxiaoNeural", rate="-6%", pitch="-12Hz"
                ).save(out)
            if os.path.exists(out) and os.path.getsize(out) >= 1000:
                segs.append(
                    {
                        "type": "record",
                        "data": {"file": "file:///" + out.replace("\\", "/")},
                    }
                )
        return segs

    @staticmethod
    def _img_segment(path: str) -> dict:
        """本地图片 -> base64 段（免路径转义、免 NapCat 找文件）

        sub_type=1 是关键：NapCat 会把它映射成 picSubType，QQ 才会当成
        「表情」渲染（单条纯表情就是超大表情）。不传就是普通图片。
        """
        with open(path, "rb") as f:
            return {
                "type": "image",
                "data": {
                    "file": "base64://" + base64.b64encode(f.read()).decode(),
                    "sub_type": 1,
                    "summary": "[动画表情]",
                },
            }

    @staticmethod
    def _retcode_error(resp) -> str:
        """OneBot 发送失败经常以 retcode!=0 正常返回而不抛异常。
        不查这个就会「假成功」——工具回报已发送、对方什么都没收到。"""
        if not isinstance(resp, dict):
            return ""
        code = resp.get("retcode")
        if code in (0, None):
            return ""
        return str(resp.get("message") or resp.get("wording") or f"retcode={code}")[:120]

    @staticmethod
    def _send_target(event: AstrMessageEvent) -> tuple[str, dict]:
        """当前会话该用哪个 OneBot action、带哪些路由参数（后台任务复用）。"""
        params: dict = {}
        sid = str(event.get_self_id() or "")
        if sid.isdigit():
            params["self_id"] = int(sid)
        gid = str(event.get_group_id() or "")
        if gid.isdigit():
            params["group_id"] = int(gid)
            return "send_group_msg", params
        uid = str(event.get_sender_id() or "")
        if not uid.isdigit():
            raise RuntimeError("拿不到会话 ID")
        params["user_id"] = int(uid)
        return "send_private_msg", params

    async def _push_segments(self, event: AstrMessageEvent, segs: list[dict]) -> None:
        """直连 OneBot action 发消息。
        context.send_message 内部走 send_by_session(event=None)，不带 self_id 路由，
        多点连接（NapCat + 测试桥/多账号）时必然 ApiNotAvailable——工具里一律用这个。
        """
        platform = str(event.get_platform_name() or "").lower()
        if platform in ("wechatauto", "weixin_oc"):
            # 微信桥没有 OneBot api：走 event.send，由桥把 Record/Image 交给 io-wechat
            chain = MessageChain()
            for seg in segs:
                kind = str(seg.get("type") or "")
                data = seg.get("data") or {}
                ref = str(data.get("file") or data.get("path") or "")
                if kind == "record":
                    chain.chain.append(Record(file=ref))
                elif kind == "file":
                    chain.chain.append(File(file=ref))
                elif kind == "image":
                    chain.chain.append(Image(file=ref))
                elif kind in ("text", "plain"):
                    chain.chain.append(Plain(str(data.get("text") or "")))
            await event.send(chain)
            return None
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前环境拿不到 QQ 客户端")
        action, params = self._send_target(event)
        params["message"] = segs
        res = await bot.api.call_action(action, **params)
        err = self._retcode_error(res)
        if err:
            raise RuntimeError(f"{action} 被拒绝：{err}")
        try:
            rid = (res or {}).get("data", {}).get("message_id") if isinstance(res, dict) else None
            logger.debug(f"[basic_tools] {action} -> {rid}")
        except Exception:  # noqa: BLE001
            pass
        return res

    async def _remember_sent(self, umo: str, text: str) -> None:
        """把自己绕过 pipeline 直发的消息补写进会话，下一轮模型才知道「已经发过了」。
        只追加历史，不触发任何 LLM 请求。"""
        if not umo or not text:
            return
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await mgr.db.get_conversation_by_id(cid=cid)
            history = list((conv.content if conv else None) or [])
            history.append({
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            })
            await mgr.db.update_conversation(cid=cid, content=history)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[basic_tools] 补写会话失败: {type(e).__name__}: {e}")

    def _at_seg(self, event: AstrMessageEvent) -> dict | None:
        """群聊里要提醒本人时用的 @ 段；私聊返回 None。"""
        if "GroupMessage" not in str(event.unified_msg_origin or ""):
            return None
        uid = str(event.get_sender_id() or "")
        return {"type": "at", "data": {"qq": uid}} if uid else None

    async def _notify_at(self, event: AstrMessageEvent, text: str) -> None:
        """把「需要用户下一步动作」的提示直接发出去，群聊带上 @。"""
        seg = self._at_seg(event)
        segs = ([seg] if seg else []) + [{"type": "text", "data": {"text": text}}]
        await self._push_segments(event, segs)
        self._queue_memo(event.unified_msg_origin, text)

    def _queue_memo(self, umo: str, text: str) -> None:
        """本轮还没结束时先攒着，等 reply 发完、历史落盘后再补写。"""
        if umo and text:
            self._memo.setdefault(umo, []).append((time.time(), text))

    @filter.after_message_sent()
    async def flush_memos(self, event: AstrMessageEvent):
        """把本轮直发的消息补进会话：模型下一轮就知道自己发过，但不会再调一次 LLM。"""
        umo = event.unified_msg_origin
        pending = self._memo.pop(umo, None)
        if not pending:
            return
        for _, text in pending:
            await self._remember_sent(umo, text)

    @filter.on_decorating_result()
    async def _mute_sing_echo(self, event: AstrMessageEvent):
        """音频已发出、或「要用户动手」的提示已 @ 发过：模型再补的话一律吞掉。

        注意：result_decorate 阶段只认「消息链被清空」。stop_event() 只是中断
        后续装饰钩子，管道照样会进 respond 阶段把这条发出去（日志里就是
        “stopped event propagation” 紧跟 “Prepare to send”）。所以这里必须
        清链 + clear_result，才算系统层真正拦住，而不是靠提示词求模型别说。
        """
        if getattr(event, "_fy_sing_sent", False) \
                or getattr(event, "_fy_sing_prompt", False) \
                or getattr(event, "_fy_voice_sent", False):
            result = event.get_result()
            if result is not None:
                result.chain.clear()
            event.clear_result()
            event.stop_event()

    async def _sweep_memos(self, older_than: float = 40.0) -> None:
        """兜底：这轮如果模型一句都没回（不会触发 after_message_sent），
        攒下的也要补写，否则它下一轮还是会以为没发。

        阈值取得比一整轮 LLM 长，免得抢在历史落盘前写、又被覆盖掉。
        """
        now = time.time()
        for umo in list(self._memo):
            items = self._memo[umo]
            due = [t for ts, t in items if now - ts >= older_than]
            if not due:
                continue
            rest = [(ts, t) for ts, t in items if now - ts < older_than]
            if rest:
                self._memo[umo] = rest
            else:
                self._memo.pop(umo, None)
            for text in due:
                await self._remember_sent(umo, text)

    async def _open_listen_window(self, event, group_id: str, peer_id: str) -> None:
        """主动私聊成功后，通知 hub 开一个监听窗口：原群 AI 继续盯着这个人的私聊。

        默认 300 秒，可用环境变量 LISTEN_WINDOW_SECONDS 覆盖。失败不影响发送。
        """
        gid = str(group_id or "").strip()
        peer = str(peer_id or "").strip()
        if not gid.isdigit() or not peer.isdigit():
            return
        try:
            seconds = int(os.environ.get("LISTEN_WINDOW_SECONDS") or 300)
        except ValueError:
            seconds = 300
        try:
            import asyncio
            import json as _json
            import urllib.request

            body = _json.dumps(
                {
                    "peer_id": peer,
                    "group_id": gid,
                    "group_umo": str(getattr(event, "unified_msg_origin", "") or ""),
                    "seconds": seconds,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:23408/windows/start",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=2)
            logger.info(f"[basic_tools] 已开启监听窗口：群 {gid} ↔ 私聊 {peer}（{seconds}s）")
        except Exception as error:  # noqa: BLE001
            logger.debug(f"[basic_tools] 开启监听窗口失败：{type(error).__name__}: {error}")

    async def _push_private_segments(
        self, event: AstrMessageEvent, raw: str, segs: list[dict]
    ) -> str:
        """把消息段私聊发给指定的人（QQ号或昵称）。返回一句人话说明。

        通道顺序要看「对方是不是好友」：
        - 好友：直接 send_private_msg
        - 非好友：优先群临时会话。QQ 对陌生人的普通私聊只会塞进对方的
          「陌生人消息」，很多人压根看不到（实际观感就是「没发出去」），
          而临时会话是对方能正常看到的窗口。临时会话也不行才退回直发。
        每条通道都查 retcode：NapCat 拒绝时经常 retcode!=0 正常返回而不抛异常，
        不查就会「工具说已发、QQ 里什么都没有」。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("当前环境拿不到 QQ 客户端")
        raw = (raw or "").strip()
        if not raw:
            raise RuntimeError("要发给谁？给个 QQ 号或昵称")
        gid = str(event.get_group_id() or "")
        temp_gid = ""
        uid = ""
        if raw.isdigit():
            uid = raw
        else:
            uid = await self._resolve_friend(bot, raw)
            if not uid and gid.isdigit():
                uid = await self._resolve_member(bot, gid, raw) or ""
                temp_gid = gid if uid else ""
            if not uid:
                found = await self._find_member_anywhere(bot, raw)
                if found:
                    uid, temp_gid = found
            if not uid:
                raise RuntimeError(
                    f"没找到叫「{raw}」的人（好友和群成员里都没有），给个 QQ 号试试"
                )
        if not uid.isdigit():
            raise RuntimeError(f"「{raw}」没解析出有效 QQ 号")
        segs = list(segs)

        async def _direct() -> str:
            resp = await bot.api.call_action(
                "send_private_msg", **{"user_id": int(uid), "message": segs}
            )
            err = self._retcode_error(resp)
            if err:
                raise RuntimeError(f"直发私聊被拒：{err}")
            await self._open_listen_window(event, gid, uid)
            return f"已私聊发给 {uid}"

        async def _temp(group_id: str) -> str:
            resp = await bot.api.call_action(
                "send_private_msg",
                **{"user_id": int(uid), "group_id": int(group_id), "message": segs},
            )
            err = self._retcode_error(resp)
            if err:
                raise RuntimeError(f"群临时会话被拒：{err}")
            await self._open_listen_window(event, group_id, uid)
            return f"已用群临时会话（群 {group_id}）发给 {uid}"

        if await self._is_friend(bot, uid):
            try:
                return await _direct()
            except Exception as first:  # noqa: BLE001
                tg = temp_gid or await self._find_shared_group(bot, uid)
                if tg:
                    try:
                        return await _temp(tg)
                    except Exception as second:  # noqa: BLE001
                        raise RuntimeError(
                            f"好友私聊和临时会话都没发出去（{uid}）："
                            f"{type(second).__name__}: {str(second)[:100]}"
                        ) from second
                raise RuntimeError(
                    f"私聊没发出去（{uid}）：{type(first).__name__}: {str(first)[:100]}"
                ) from first

        tg = temp_gid or await self._find_shared_group(bot, uid)
        if tg:
            try:
                return await _temp(tg)
            except Exception as first:  # noqa: BLE001
                try:
                    note = await _direct()
                except Exception as second:  # noqa: BLE001
                    raise RuntimeError(
                        f"临时会话和直发都没发出去（{uid}）："
                        f"{type(first).__name__}: {str(first)[:80]} / "
                        f"{type(second).__name__}: {str(second)[:80]}"
                    ) from second
                return note + "（不是对方好友，QQ 可能把它收进「陌生人消息」）"
        try:
            note = await _direct()
        except Exception as first:  # noqa: BLE001
            raise RuntimeError(
                f"没找到共同群，直发也被拒（{uid}）："
                f"{type(first).__name__}: {str(first)[:100]}。"
                "对方不是你好友，让他先回你一条消息或拉个群"
            ) from first
        return note + "（不是对方好友，QQ 可能把它收进「陌生人消息」）"

    @filter.llm_tool(name="send_to_group")
    async def tool_send_to_group(
        self,
        event: AstrMessageEvent,
        group_id: str,
        content: str,
        at_name: str = "",
        as_voice: str = "",
    ):
        """往指定群发一条消息（跨群发送）。默认发文字；想发语音就把 as_voice 填「语音」，content 会合成语音发过去。

        Args:
            group_id(string): QQ 群号，纯数字
            content(string): 要发的内容（发语音时就是要念出来的话，200 字以内）
            at_name(string): 要 @ 的群成员昵称或 QQ，不需要 @ 就留空
            as_voice(string): 发语音填「语音」（或 true）；发文字留空
        """
        gid = re.sub(r"\D", "", str(group_id or ""))
        content = (content or "").strip()
        if len(gid) < 6 or not content:
            return "群号不对或内容是空的"
        if len(content) > 500:
            return "内容太长，控制在 500 字以内"
        if (denied := _daily_gate("发群", 10)):
            return denied
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        segs: list[dict] = []
        at_name = (at_name or "").strip()
        if at_name:
            uid = await self._resolve_member(bot, gid, at_name)
            if not uid:
                return f"群 {gid} 里没找到「{at_name}」"
            segs.append({"type": "at", "data": {"qq": uid}})
        if _voice_flag(as_voice):
            if (denied := _daily_gate("发语音", 20)):
                return denied
            try:
                voice_segs = await self._voice_segments(content)
            except Exception as e:  # noqa: BLE001
                return f"语音合成失败: {type(e).__name__}: {str(e)[:100]}"
            if not voice_segs:
                return "语音生成失败了，再试一次？"
            segs.extend(voice_segs)
        else:
            segs.append({"type": "text", "data": {"text": content}})
        try:
            res = await bot.api.call_action(
                "send_group_msg", **{"group_id": int(gid), "message": segs}
            )
            if (err := self._retcode_error(res)):
                return f"发群失败：{err}"
            return f"已发到群 {gid}" + ("（语音）" if _voice_flag(as_voice) else "")
        except Exception as e:  # noqa: BLE001
            return f"发群失败: {type(e).__name__}: {str(e)[:100]}"

    @filter.llm_tool(name="send_private")
    async def tool_send_private(
        self,
        event: AstrMessageEvent,
        target: str,
        content: str,
        as_voice: str = "",
    ):
        """给某个好友发私聊消息。默认发文字；想发语音就把 as_voice 填「语音」，content 会合成语音发过去。

        Args:
            target(string): QQ 号或群里的昵称
            content(string): 要发的内容（发语音时就是要念出来的话，200 字以内）
            as_voice(string): 发语音填「语音」（或 true）；发文字留空
        """
        target = (target or "").strip()
        content = (content or "").strip()
        if not target or not content:
            return "目标或内容是空的"
        if len(content) > 500:
            return "内容太长，控制在 500 字以内"
        if (denied := _daily_gate("私聊", 10)):
            return denied
        segs: list[dict] = []
        if _voice_flag(as_voice):
            if (denied := _daily_gate("发语音", 20)):
                return denied
            try:
                segs = await self._voice_segments(content)
            except Exception as e:  # noqa: BLE001
                return f"语音合成失败: {type(e).__name__}: {str(e)[:100]}"
            if not segs:
                return "语音生成失败了，再试一次？"
        else:
            segs = [{"type": "text", "data": {"text": content}}]
        try:
            note = await self._push_private_segments(event, target, segs)
            return f"{note}：{'语音' if _voice_flag(as_voice) else content[:40]}"
        except Exception as e:  # noqa: BLE001
            return f"私聊失败: {type(e).__name__}: {str(e)[:100]}"


exit_plugin_main(globals(), _REAL_MODULE_NAME)
