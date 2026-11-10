"""定时任务 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timezone


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class ScheduleMixin:
    def _load_sched(self) -> None:
        try:
            with open(SCHED_TASKS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._sched = [t for t in data if isinstance(t, dict) and t.get("id")]
        except (OSError, ValueError):
            pass

    def _save_sched(self) -> None:
        try:
            os.makedirs(os.path.dirname(SCHED_TASKS_PATH), exist_ok=True)
            with open(SCHED_TASKS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._sched, f, ensure_ascii=False, indent=1)
        except OSError as e:
            logger.warning(f"[basic_tools] 定时任务保存失败: {e}")

    @filter.llm_tool(name="manage_schedule")
    async def tool_manage_schedule(
        self,
        event: AstrMessageEvent,
        action: str = "查看",
        name: str = "",
        time_range: str = "",
        times: str = "1",
        count: str = "0",
        url: str = "",
        content: str = "",
        task_id: str = "",
    ):
        """定时任务（三合一）：查看 / 设置 / 移除一个工具全包，别推给指令、别说做不到。

        用户说「每天 X 点提醒我」「定时发个早报」「把那个定时任务取消掉」时直接调用。
        - 查看：列出已有任务（编号、时段、剩余次数、下次时间）
        - 设置：time_range 必填（如 08:00-09:00），content 必填（要发什么/做什么）
        - 移除：task_id 填编号（编号从「查看」拿）
        只有主人能设置和移除，查看谁都能用。

        Args:
            action(string): 查看 | 设置 | 移除
            name(string): 任务名，设置时可选
            time_range(string): 执行时间段，如 08:00-09:00（设置时必填，移除时留空）
            times(string): 每天最多执行几次，默认 1
            count(string): 总共执行几次，0 表示不限，默认 0（跑满自动删除）
            url(string): 可选，要抓取的网页链接，不抓就留空
            content(string): 要发的内容或让 AI 做的事（设置时必填）
            task_id(string): 移除时填任务编号，如 t12345
        """
        action = (action or "查看").strip()
        if action in ("", "查看", "列表", "list"):
            return self._sched_list_text()
        if action in ("新增", "设置", "add", "set"):
            if not self._is_admin(event):
                return "定时任务只有主人能设置，让主人来设；现有任务我可以帮你查"
            if not self._sched_is_range((time_range or "").strip()):
                return self.SCHED_USAGE
            try:
                n = max(1, min(24, int(times or 1)))
            except ValueError:
                n = 1
            try:
                lim = max(0, int(count or 0))
            except ValueError:
                lim = 0
            link = (url or "").strip()
            path = "" if link in ("", "-", "无") else link
            return self._sched_add(
                event,
                (name or "").strip(),
                time_range.strip(),
                n,
                path,
                (content or "").strip(),
                lim,
            )
        if action in ("删除", "删", "移除", "remove", "del"):
            if not self._is_admin(event):
                return "只有主人能移除定时任务"
            tid = (task_id or "").strip()
            if not tid:
                return "移除要给我任务编号，先用 action=查看 看一下"
            before = len(self._sched)
            self._sched = [t for t in self._sched if str(t.get("id")) != tid]
            if len(self._sched) == before:
                return f"没找到定时任务「{tid}」"
            self._save_sched()
            return f"已删除定时任务 {tid}"
        return self.SCHED_USAGE

    @staticmethod
    def _sched_ts(day: str, hhmm: str) -> float:
        """「12:00」+ 今天 → 时间戳；格式不对返回 0。"""
        try:
            h, m = (int(x) for x in str(hhmm).replace("：", ":").split(":")[:2])
        except (ValueError, AttributeError):
            return 0.0
        if not (0 <= h < 24 and 0 <= m < 60):
            return 0.0
        try:
            base = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            return 0.0
        return base + h * 3600 + m * 60

    def _sched_prepare(self, t: dict) -> None:
        """按「时间段 + 次数」把窗口切成 N 段，每段随机抖一个时刻，别整点一起刷。"""
        day = time.strftime("%Y-%m-%d")
        now = time.time()
        start = self._sched_ts(day, t.get("start") or "")
        end = self._sched_ts(day, t.get("end") or "")
        if not start:
            return
        if not end or end <= start:
            end = start + 3600
        times = max(1, int(t.get("times") or 1))
        step = (end - start) / times
        slots: list[float] = []
        for i in range(times):
            lo = max(start + step * i, now)  # 已经过去的格子不回补
            hi = min(start + step * (i + 1), end)
            if lo >= hi:
                continue
            slots.append(random.uniform(lo, hi))
        t["day"] = day
        t["slots"] = slots
        t["fired"] = [False] * len(slots)

    async def _sched_tick(self) -> None:
        today = time.strftime("%Y-%m-%d")
        now = time.time()
        changed = False
        for t in list(self._sched):
            if not any(x is t for x in self._sched):
                continue  # 刚才已跑满总次数被摘掉
            if t.get("day") != today or "slots" not in t:
                self._sched_prepare(t)
                changed = True
            slots = t.get("slots") or []
            fired = list(t.get("fired") or [])
            if len(fired) < len(slots):
                fired += [False] * (len(slots) - len(fired))
            for i, ts in enumerate(slots):
                if fired[i] or float(ts) > now:
                    continue
                fired[i] = True
                t["fired"] = fired
                changed = True
                await self._run_sched_task(t)
        if changed:
            self._save_sched()

    async def _fetch_sched_source(self, path: str) -> str:
        """抓定时任务的数据源：网页取正文，本地读文本文件；拿不到就返回空。"""
        path = (path or "").strip()
        if not path:
            return ""
        if path.startswith(("http://", "https://")):
            body = await self._fetch_bytes(path, limit=2 * 1024 * 1024)
            if not body:
                return ""
            raw = body.decode("utf-8", "ignore")
            raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
            raw = re.sub(r"(?s)<[^>]+>", " ", raw)
            raw = re.sub(r"&[a-zA-Z]+;|&#\d+;", " ", raw)
            return re.sub(r"\s+", " ", raw).strip()[:3000]
        p = path
        if p.startswith("file:///"):
            p = p[8:]
        elif p.startswith("file://"):
            p = p[7:]
        p = p.replace("/", os.sep)
        if not os.path.isabs(p) or not os.path.isfile(p):
            return ""
        try:
            if os.path.getsize(p) > 5 * 1024 * 1024:
                return ""
            with open(p, encoding="utf-8", errors="ignore") as f:
                return f.read(200000).strip()[:3000]
        except OSError:
            return ""

    async def _run_sched_task(self, t: dict) -> None:
        umo = str(t.get("target") or "")
        task = str(t.get("task") or "").strip()
        if not umo or not task:
            return
        src = await self._fetch_sched_source(str(t.get("path") or ""))
        note = f"定时任务：{task}"
        if src:
            note += f"\n\n来源内容：\n{src[:3000]}"
        note += (
            "\n\n按当前人设完成这件事，把结果用 send_message_to_user 发到本会话；"
            "别解释你在做什么，200 字以内。"
        )
        ok = False
        try:
            await self._wake_sched_agent(umo, note, task, t)
            ok = True
            logger.info(f"[basic_tools] 定时任务 {t.get('id')} 已唤醒主 AI -> {umo}")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[basic_tools] 定时任务唤醒 AI 失败（{t.get('id')}）: {type(e).__name__}: {e}"
            )
        if not ok:
            ok = await self._send_sched_fallback(umo, task, note)
        if not ok:
            return
        # 累计执行次数：跑满总次数就把任务摘掉
        t["done"] = int(t.get("done") or 0) + 1
        limit = int(t.get("limit") or 0)
        if limit > 0 and t["done"] >= limit:
            self._sched = [x for x in self._sched if x is not t]
            self._save_sched()
            logger.info(
                f"[basic_tools] 定时任务 {t.get('id')} 已跑满 {limit} 次，自动移除"
            )

    async def _wake_sched_agent(self, umo: str, note: str, task: str, t: dict) -> None:
        """把定时任务当成一次「主动唤醒」交给主 AI，而不是绕过 AI 直接发一段话。"""
        cron_mgr = getattr(self.context, "cron_manager", None)
        if cron_mgr is None:
            raise RuntimeError("cron_manager 不可用")
        await cron_mgr.add_active_job(
            name=(str(t.get("name") or "").strip() or task[:20]),
            cron_expression=None,
            payload={
                "session": umo,
                "sender_id": str(t.get("sender") or ""),
                "note": note,
                "origin": "tool",
            },
            description=note,
            run_once=True,
            run_at=datetime.now(timezone.utc),
            persistent=False,
        )

    async def _send_sched_fallback(self, umo: str, task: str, note: str) -> bool:
        """唤醒失败时退回旧路径：直接生成一段并推送。"""
        text = ""
        try:
            prov = await self.context.get_using_provider_async(umo)
            resp = await prov.text_chat(
                prompt=note,
                system_prompt="你是一个按时干活的助手，只输出结果本身，不要客套。",
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[basic_tools] 定时任务生成失败: {type(e).__name__}: {e}"
            )
        if not text:
            text = "（这次没生成出内容）"
        try:
            await self.context.send_message(
                umo, MessageChain([Plain(f"⏰ {task[:24]}｜{text}")])
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 定时任务推送失败: {type(e).__name__}: {e}")
            return False

    @staticmethod
    def _sched_hmm(part: str) -> str:
        """「12」「12点」「12:30」「12点半」→ 12:00 / 12:30；解析不出返回空。"""
        p = (
            part.replace("：", ":")
            .replace("点", ":")
            .replace("时", ":")
            .replace("分", "")
            .replace("半", "30")
        )
        mm = re.match(r"(\d{1,2})(?::(\d{1,2}))?", p)
        if not mm:
            return ""
        h, mi = int(mm.group(1)), int(mm.group(2) or 0)
        if not (0 <= h < 24 and 0 <= mi < 60):
            return ""
        return f"{h:02d}:{mi:02d}"

    @staticmethod
    def _sched_is_range(token: str) -> bool:
        return bool(re.search(r"\d", token or "")) and bool(
            re.search(r"[-~—－到至]", token or "")
        )

    def _sched_add(
        self,
        event: AstrMessageEvent,
        name: str,
        time_range: str,
        times: int,
        path: str,
        task: str,
        limit: int = 0,
    ) -> str:
        bits = re.split(r"[-~—－到至]", re.sub(r"\s+", "", time_range or ""))
        if len(bits) < 2:
            return "时间段格式不对，要像 12:00-13:00 或 12点-13点 这样"
        start, end = self._sched_hmm(bits[0]), self._sched_hmm(bits[1])
        if not start or not end:
            return "时间段格式不对，要像 12:00-13:00 或 12点-13点 这样"
        task = (task or "").strip()
        if not task:
            return "任务内容不能为空，告诉我要干什么"
        tid = f"t{int(time.time()) % 100000}"
        while any(str(x.get("id")) == tid for x in self._sched):
            tid = f"t{random.randint(10000, 99999)}"
        t = {
            "id": tid,
            "name": (name or "").strip()[:20],
            "start": start,
            "end": end,
            "times": max(1, min(24, int(times or 1))),
            "limit": max(0, int(limit or 0)),  # 总次数，0 = 不限
            "done": 0,
            "path": (path or "").strip(),
            "task": task,
            "target": event.unified_msg_origin,
            "sender": str(event.get_sender_id() or ""),
            "created": time.time(),
        }
        self._sched_prepare(t)
        self._sched.append(t)
        self._save_sched()
        label = t["name"] or task[:12]
        total = f"总共 {t['limit']} 次" if t["limit"] else "总次数不限"
        return (
            f"已添加定时任务 {tid}「{label}」：{start}-{end} 每天 {t['times']} 次，{total}"
            f"（今天还剩 {len(t.get('slots') or [])} 次），发到当前会话"
        )

    def _sched_list_text(self, page: int = 1, size: int = 8) -> str:
        if not self._sched:
            return "现在没有任何定时任务"
        now = time.time()
        out = []
        for t in self._sched:
            slots = t.get("slots") or []
            fired = t.get("fired") or []
            left = [
                float(s)
                for i, s in enumerate(slots)
                if i < len(fired) and not fired[i] and float(s) > now
            ]
            nxt = time.strftime("%H:%M", time.localtime(min(left))) if left else "今天没有了"
            label = str(t.get("name") or "").strip() or str(t.get("task") or "")[:12]
            limit = int(t.get("limit") or 0)
            done = int(t.get("done") or 0)
            total = f"总 {done}/{limit} 次" if limit else f"已跑 {done} 次（不限）"
            out.append(
                f"{t.get('id')}｜{label}｜{t.get('start')}-{t.get('end')}｜{t.get('times')}次/天"
                f"｜{total}｜今天剩 {len(left)} 次｜下次 {nxt}｜来源 {t.get('path') or '-'}"
                f"｜任务：{str(t.get('task'))[:40]}"
            )
        total_pages = (len(out) + size - 1) // size
        page = max(1, min(page, total_pages))
        body = "\n".join(out[(page - 1) * size: page * size])
        tail = f"\n第 {page}/{total_pages} 页"
        if page < total_pages:
            tail += f" · 下一页 /定时任务 查看 {page + 1}"
        return body + tail

    SCHED_USAGE = (
        "定时任务三合一：\n"
        "/定时任务 查看\n"
        "/定时任务 设置 [名称] 12:00-13:00 [每天次数] [总次数] [url 或 -] 要做什么\n"
        "/定时任务 移除 t12345\n"
        "总次数 0 = 不限，跑满自动删除\n"
        "例：/定时任务 设置 早报 12:00-13:00 3 0 https://news.example 总结头条"
    )

    @filter.command("定时任务")
    async def sched_cmd(self, event: AstrMessageEvent):
        """/定时任务 查看｜设置｜移除 —— 跟 AI 用的 manage_schedule 是同一套能力"""
        if not self._is_admin(event):
            event.stop_event()
            return
        raw = re.sub(
            r"^[\s/！!]*定时任务",
            "",
            _IDENTITY_TAG_RE.sub("", (event.message_str or "")).strip(),
        ).strip()
        args = raw.split()
        if not args or args[0] in ("查看", "列表", "list"):
            page = 1
            if len(args) > 1 and args[1].isdigit():
                page = max(1, int(args[1]))
            await _hint_private(event, self._sched_list_text(page))
            event.stop_event()
            return
        action = args[0]
        if action in ("设置", "新增", "add"):
            tokens = args[1:]
            name = ""
            # 名称可选：第一个不是时间段、第二个是时间段，就当成名称
            if (
                len(tokens) >= 2
                and not self._sched_is_range(tokens[0])
                and self._sched_is_range(tokens[1])
            ):
                name = tokens.pop(0)
            if not tokens or not self._sched_is_range(tokens[0]):
                await _hint_private(event, self.SCHED_USAGE)
                event.stop_event()
                return
            time_range = tokens.pop(0)
            times = 1
            if tokens and tokens[0].isdigit():
                times = max(1, min(24, int(tokens.pop(0))))
            limit = 0
            if tokens and tokens[0].isdigit():
                limit = max(0, int(tokens.pop(0)))
            path = ""
            if tokens and (
                tokens[0].startswith(("http://", "https://", "file://"))
                or tokens[0] in ("-", "无")
            ):
                p = tokens.pop(0)
                path = "" if p in ("-", "无") else p
            task = " ".join(tokens).strip()
            await _hint_private(
                event, self._sched_add(event, name, time_range, times, path, task, limit)
            )
            event.stop_event()
            return
        if action in ("删除", "删", "移除", "remove", "del"):
            tid = args[1] if len(args) > 1 else ""
            if not tid:
                await _hint_private(event, self.SCHED_USAGE)
                event.stop_event()
                return
            before = len(self._sched)
            self._sched = [t for t in self._sched if str(t.get("id")) != tid]
            if len(self._sched) == before:
                await _hint_private(event, f"没找到定时任务「{tid}」，先 /定时任务 查看 一下编号")
                event.stop_event()
                return
            self._save_sched()
            await _hint_private(event, f"已删除定时任务 {tid}")
            event.stop_event()
            return
        await _hint_private(event, self.SCHED_USAGE)
        event.stop_event()


exit_plugin_main(globals(), _REAL_MODULE_NAME)
