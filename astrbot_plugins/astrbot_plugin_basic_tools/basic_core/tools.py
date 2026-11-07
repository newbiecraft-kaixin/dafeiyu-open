"""通用 LLM 工具 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import re
import time

import aiohttp

from astrbot.api.event import AstrMessageEvent, filter

from . import blocklist
from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


def emoji_like_error(resp) -> str:
    """从 set_msg_emoji_like 的返回里取真实失败原因（NapCat 会 retcode=0 但 result!=0）。"""
    data = resp
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if not isinstance(data, dict):
        return ""
    err = str(data.get("errMsg") or "").strip()
    if err:
        return err
    result = data.get("result")
    if result not in (None, 0, "0"):
        return f"result={result}"
    return ""


class ToolsMixin:
    @filter.llm_tool(name="get_current_time")
    async def get_current_time(self, event: AstrMessageEvent):
        """获取当前的日期和时间。当用户问现在几点、今天几号、时间相关计算时使用。

        Args:
            无参数
        """
        return time.strftime("现在的时间是：%Y年%m月%d日 %H:%M:%S %A")

    @filter.llm_tool(name="get_group_members")
    async def get_group_members(self, event: AstrMessageEvent, group_id: str = ""):
        """获取群成员列表，包括每个人的 QQ 号、昵称/群名片和身份（群主/管理员/成员）。当用户问"谁是群主"、"群里有谁"、"查某人QQ号"、"管理是谁"时使用。

        Args:
            group_id(string): 要查询的群号；留空表示查询当前会话所在的群
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        gid = (group_id or "").strip() or str(event.get_group_id() or "")
        if not gid:
            return "这不是群聊，也没有指定群号"
        try:
            members = await bot.get_group_member_list(group_id=int(gid))
        except Exception as e:
            return f"查询群 {gid} 成员失败: {e}"
        role_map = {"owner": "群主", "admin": "管理员"}
        lines = []
        for m in members:
            role = role_map.get(m.get("role", ""), "成员")
            name = m.get("card") or m.get("nickname") or "-"
            lines.append(f"[{role}] {name} (QQ:{m.get('user_id')})")
        return f"群 {gid} 共 {len(lines)} 人：\n" + "\n".join(lines)

    @filter.llm_tool(name="group_poke")
    async def group_poke(self, event: AstrMessageEvent, user_id: str = "", group_id: str = ""):
        """在群里戳一戳（拍一拍）某个人，像真人互动一样提醒对方。当用户说"戳戳他/拍拍他"，或你想俏皮地引起某人注意时使用。注意：只应按群里用户的明确要求去戳对应的人，不要乱戳。

        Args:
            user_id(string): 要戳的人的QQ号或群名片/昵称（支持部分匹配，不用问用户QQ号）；留空表示当前说话的人
            group_id(string): 群号；留空表示当前群
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        uid = (user_id or "").strip() or str(event.get_sender_id() or "")
        gid = (group_id or "").strip() or str(event.get_group_id() or "")
        if not uid or not gid:
            return "缺少 QQ 号或群号，先问清楚要戳谁"
        if not uid.isdigit():
            resolved = await self._resolve_member(bot, gid, uid)
            if not resolved:
                return f"在群里没找到叫「{uid}」的人，可以调 get_group_members 查名单"
            uid = resolved
        if (denied := _daily_gate("群戳一戳", 20)):
            return denied
        try:
            await bot.api.call_action(
                "group_poke", **{"group_id": int(gid), "user_id": int(uid)}
            )
            return f"已经在群 {gid} 戳了 {uid} 一下"
        except Exception as e:
            return f"戳一戳失败: {e}"

    @filter.llm_tool(name="get_weather")
    async def get_weather(self, event: AstrMessageEvent, city: str = ""):
        """查询某个城市的当前天气（温度/天气/风/湿度）和今日温度范围。问天气、冷不冷、要不要带伞时必须用这个。

        Args:
            city(string): 城市名，如 福州、北京；省市连写也行，如 福建龙岩。用户这轮没说城市就留空，绝对不要自己猜一个
        """
        city = (city or "").strip()
        default_city = _weather_default_city()
        try:
            user_text = event.message_str or ""
        except Exception:  # noqa: BLE001
            user_text = ""
        if not city:
            if default_city:
                city = default_city
            else:
                return (
                    "用户这轮没说城市。先问一句「你在哪儿呀」再查，别自己挑一个城市。"
                )
        elif not default_city and user_text:
            # 模型自己挑的城市：用户这轮没提过，也不在默认城市里 → 先别查，问清楚
            hit = city in user_text or (
                len(city) >= 4 and (city[:2] in user_text or city[-2:] in user_text)
            )
            if not hit:
                return (
                    f"用户这轮没提过「{city}」，这是你自己猜的。别猜城市，"
                    "先问他在哪个城市（同一个群里各人城市不一样）。"
                )
        # 复合地名（省+市、市+县）整串常查不到：把 2 字片段也试一遍，
        # 按行政级别+人口取最像城市的那个（避免「龙岩武平」只查到广西那个小地方）
        cands = [city]
        for suf in ("市", "县", "区", "镇", "乡"):
            if city.endswith(suf):
                cands.append(city[:-1])
                break
        if len(city) >= 4:
            cands += [city[:2], city[-2:]]
        tier = {"PPLC": 4, "PPLA": 4, "PPLA2": 3, "PPLA3": 2, "PPLA4": 1}
        try:
            async with aiohttp.ClientSession() as sess:
                best = None  # (score, record)
                seen = set()
                for name in dict.fromkeys(cands):
                    async with sess.get(
                        "https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": name, "count": 5, "language": "zh"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        geo = await r.json()
                    for rec in (geo or {}).get("results") or []:
                        key = (rec.get("latitude"), rec.get("longitude"))
                        if key in seen:
                            continue
                        seen.add(key)
                        score = tier.get(rec.get("feature_code"), 0) * 10_000_000 + (
                            rec.get("population") or 0
                        )
                        if best is None or score > best[0]:
                            best = (score, rec)
                    if best and best[1].get("feature_code") in ("PPLC", "PPLA", "PPLA2"):
                        break  # 已经是市级，不用再往下试
                if best is None:
                    return (
                        f"没查到「{city}」的天气数据。别猜温度，直接跟用户说没查到，"
                        "让他换个更常见的市名（比如只报市名）。"
                    )
                rec = best[1]
                lat, lon = rec["latitude"], rec["longitude"]
                name = rec.get("name") or city
                a1, a2 = (rec.get("admin1") or "").strip(), (rec.get("admin2") or "").strip()
                region = a2 if a2 and name not in a2 else a1
                if region in (name, name + "市", name + "县", name + "区"):
                    region = a1 if a1 and a1 not in (region, name) else ""
                label = f"{name}（{region}）" if region and region != name else name
                async with sess.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                        "daily": "temperature_2m_max,temperature_2m_min",
                        "forecast_days": 1,
                        "timezone": "auto",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    wx = await r.json()
            cur = wx["current"]
            desc = WMO_CODES.get(int(cur.get("weather_code", 0)), "未知")
            d = wx.get("daily", {})
            lo = (d.get("temperature_2m_min") or ["?"])[0]
            hi = (d.get("temperature_2m_max") or ["?"])[0]
            return (
                f"{label}现在 {cur['temperature_2m']}℃，{desc}，"
                f"湿度 {cur['relative_humidity_2m']}%，风 {cur['wind_speed_10m']}km/h，"
                f"今日 {lo}~{hi}℃"
            )
        except Exception as e:
            return f"天气查询失败: {e}（跟用户说没查到就行，别编数据）"

    @filter.llm_tool(name="get_group_list")
    async def get_group_list(self, event: AstrMessageEvent):
        """列出机器人账号加入的所有 QQ 群（群号、群名、人数）。当用户要你去某个群做事情但没给群号，或问"你在哪些群"时，先调用这个找到目标群。

        Args:
            无参数
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        try:
            groups = await bot.get_group_list()
        except Exception as e:
            return f"拉取群列表失败: {e}"
        lines = []
        for g in groups:
            lines.append(f"{g.get('group_name') or '未命名群'} (群号:{g.get('group_id')}, {g.get('member_count', '?')}人)")
        return f"我一共在 {len(lines)} 个群里：\n" + "\n".join(lines)

    @filter.llm_tool(name="friend_poke")
    async def friend_poke(self, event: AstrMessageEvent, user_id: str = ""):
        """在私聊里戳一戳（拍一拍）好友。当私聊中用户说"戳戳我/拍拍我"，或你想俏皮地打招呼时使用。群里请用 group_poke。

        Args:
            user_id(string): 要戳的好友 QQ 号；留空表示当前对话的用户
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        uid = (user_id or "").strip() or str(event.get_sender_id() or "")
        if not uid:
            return "不知道要戳谁"
        if (denied := _daily_gate("好友戳一戳", 10)):
            return denied
        try:
            await bot.api.call_action("friend_poke", **{"user_id": int(uid)})
            return f"已经戳了 {uid} 一下"
        except Exception as e:
            return f"戳一戳失败: {e}"

    @filter.llm_tool(name="react_message")
    async def react_message(self, event: AstrMessageEvent, emoji: str = "", message_id: str = ""):
        """给一条消息贴上 QQ 表情回应（就是消息右下角那个可以贴的小表情）。当用户说"给这条消息点个表情/贴个吃瓜"，或你觉得某条消息用一个表情回应比打字更传神时使用。只在群聊有效：QQ 私聊不支持表情回应，别在私聊里用，也别向对方宣称已经贴上。

        Args:
            emoji(string): QQ 表情名称或 ID，如 吃瓜、点赞、doge、128
            message_id(string): 要回应的消息 ID；留空表示回应触发你这条调用的那条消息
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        emoji = (emoji or "").strip()
        if not emoji:
            return "没说要用哪个表情"
        fid = None
        if emoji.isdigit():
            fid = emoji
        else:
            try:
                with open(STICKER_MAP, encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = [p.strip() for p in line.split(":")]
                        if len(parts) >= 4 and parts[2] == "face":
                            name, meaning = parts[0], parts[1]
                            if emoji == name or emoji in name or emoji in meaning:
                                fid = str(parts[3])
                                if emoji == name:
                                    break
            except OSError:
                pass
        if not fid:
            return f"没有叫「{emoji}」的 QQ 表情"
        mid = (message_id or "").strip()
        if not mid:
            mid = str(getattr(event.message_obj, "message_id", "") or "")
        if not mid:
            return "拿不到消息 ID，没法贴表情"
        try:
            resp = await bot.api.call_action(
                "set_msg_emoji_like",
                **{"message_id": mid, "emoji_id": fid, "set": True},
            )
        except Exception as e:
            return f"贴表情回应失败: {e}"
        err = emoji_like_error(resp)
        if err:
            if not str(event.get_group_id() or "").strip():
                return f"贴表情回应失败: {err}（QQ 私聊不支持表情回应，只能贴到群消息上；私聊里直接发个表情吧）"
            return f"贴表情回应失败: {err}"
        return f"已给消息贴上表情回应（emoji:{fid}）"

    @filter.llm_tool(name="recall_own_messages")
    async def recall_own_messages(self, event: AstrMessageEvent, count: str = "1"):
        """撤回你自己最近发出去的消息（只能撤自己发的，动不了别人的）。当你说错话、发错图/发错表情包、发重了，或对方明确让你撤回时使用。不要用来刷屏式连撤。

        Args:
            count(string): 要撤回几条自己的消息，1-5，默认 1
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return "当前环境拿不到 QQ 客户端"
        try:
            n = max(1, min(5, int(str(count).strip() or "1")))
        except ValueError:
            n = 1
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        gid = str(event.get_group_id() or "").strip()
        try:
            if gid:
                hist = await bot.api.call_action(
                    "get_group_msg_history", **{"group_id": int(gid), "count": 30}
                )
            else:
                fuid = str(event.get_sender_id() or "").strip()
                if not fuid:
                    return "拿不到会话对象，没法找自己的发言"
                hist = await bot.api.call_action(
                    "get_friend_msg_history", **{"user_id": int(fuid), "count": 30}
                )
        except Exception as e:
            return f"拉取聊天记录失败: {e}"
        msgs = (hist or {}).get("messages") or []
        mine = [
            str(m.get("message_id"))
            for m in msgs
            if str((m.get("sender") or {}).get("user_id") or "") == self_id
        ]
        if not mine:
            return "最近 30 条里没找到自己发的消息，没得撤"
        ok, fail = 0, []
        for mid in mine[-n:]:
            try:
                await bot.api.call_action("delete_msg", **{"message_id": int(mid)})
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail.append(f"{mid}: {e}")
        # 条数统计：本次新增（找到的自己发言数）/ 撤销（撤回成功数），并累计
        st = getattr(self, "_recall_stats", None)
        if st is None:
            st = self._recall_stats = {"recalled": 0}
        st["recalled"] += ok
        extra = f"，撤销失败 {len(fail)} 条" if fail else ""
        return (
            f"本次找到自己最近 {len(mine)} 条发言，成功撤销 {ok} 条{extra}"
            f"（累计撤销 {st['recalled']} 条）"
        )

    @filter.llm_tool(name="ignore_user")
    async def ignore_user(
        self, event: AstrMessageEvent, user: str = "", minutes: str = "30", reason: str = ""
    ):
        """当你对某人厌烦、不想再聊时使用：把TA拉黑几分钟，期间TA发的消息你一概不回应，到期自动恢复。仅限对方持续骚扰、阴阳、刷屏或让你很不舒服时用，别滥用，也别拉黑主人。

        Args:
            user(string): 要拉黑谁，@TA或写TA的昵称
            minutes(string): 拉黑多少分钟，1-720，默认30
            reason(string): 一句话原因，如「老阴阳我」「刷屏太吵」
        """
        gid = str(event.get_group_id() or "").strip()
        bot = getattr(event, "bot", None)
        raw = str(user or "").strip()
        uid, disp = "", ""
        m = re.search(r"\b(\d{5,11})\b", raw)
        if m:
            uid, disp = m.group(1), m.group(1)
        elif raw and bot is not None and gid:
            try:
                uid = await self._resolve_member(bot, gid, raw) or ""
                disp = raw
            except Exception:  # noqa: BLE001
                uid = ""
        if not uid:
            return "没认出要拉黑谁，给个准确的昵称或QQ号"
        mins = blocklist.set_block(uid, minutes, reason=str(reason or "").strip(), by="ai")
        return f"行，{disp}（{uid}）接下来 {mins} 分钟不聊了（原因：{reason or '没说'}）。到期自动恢复，期间TA说什么我都当没看见。"


exit_plugin_main(globals(), _REAL_MODULE_NAME)
