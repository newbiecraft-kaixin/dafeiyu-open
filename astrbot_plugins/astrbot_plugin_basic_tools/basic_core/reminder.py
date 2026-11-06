"""提醒与插件生命周期 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import asyncio
import json
import os
import time


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class ReminderMixin:
    def _load_reminders(self) -> None:
        try:
            with open(REMINDERS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._reminders = data
        except (OSError, ValueError):
            pass

    def _save_reminders(self) -> None:
        try:
            os.makedirs(os.path.dirname(REMINDERS_PATH), exist_ok=True)
            with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._reminders, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[basic_tools] 提醒保存失败: {e}")

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._reminder_loop())
        try:
            self._refresh_super_faces()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 启动时刷新超级表情失败: {e}")
        logger.info(
            f"[basic_tools] 已启动：待触发提醒 {len(self._reminders)} 条，"
            f"定时任务 {len(self._sched)} 个，超级表情 {len(self._sfaces)} 个"
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                # 表情清单是动态数据：截止到期的才重读一次（默认一周），
                # 平时只读内存，不重复解析 QQ 的配置
                if time.time() - self._face_mark_ts() >= FACE_REFRESH_SECONDS:
                    self._refresh_super_faces(force=True)
                now = time.time()
                # 定时任务：看当前时段的槽位有没有到点
                try:
                    await self._sched_tick()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[basic_tools] 定时任务 tick 异常: {type(e).__name__}: {e}")
                try:
                    await self._sweep_memos()
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[basic_tools] 补写会话扫描异常: {type(e).__name__}: {e}")
                try:
                    _storage_sweep()
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[basic_tools] 存储清理异常: {type(e).__name__}: {e}")
                due = [r for r in self._reminders if float(r["due"]) <= now]
                if not due:
                    continue
                self._reminders = [
                    r for r in self._reminders if float(r["due"]) > now
                ]
                self._save_reminders()
                for r in due:
                    try:
                        await self.context.send_message(
                            r["umo"], MessageChain([Plain(f"⏰ 到点了：{r['text']}")])
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[basic_tools] 提醒发送失败: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[basic_tools] 提醒循环异常: {type(e).__name__}: {e}")

    @filter.llm_tool(name="set_reminder")
    async def set_reminder(self, event: AstrMessageEvent, minutes: str = "", text: str = ""):
        """设置一个提醒，到时间后会在这个会话里主动发消息提醒。当用户说"X分钟后提醒我……"时使用。

        Args:
            minutes(string): 多少分钟后提醒（数字）
            text(string): 提醒的内容
        """
        try:
            mins = float(str(minutes).strip())
        except (TypeError, ValueError):
            return "分钟数没写对，跟用户确认一下再说"
        text = (text or "").strip() or "（没说提醒啥）"
        if mins < 0.1 or mins > 60 * 24 * 7:
            return "提醒时间要在 1 分钟到 7 天之间"
        self._reminders.append(
            {
                "due": time.time() + mins * 60,
                "text": text[:100],
                "umo": event.unified_msg_origin,
            }
        )
        self._save_reminders()
        return f"好，{int(mins)} 分钟后提醒：{text[:40]}"


exit_plugin_main(globals(), _REAL_MODULE_NAME)
