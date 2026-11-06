"""自我成长系统：让大肥鱼具备「经验学习」闭环。

工作原理：
1. AI 在对话中学到新东西（被纠正、发现规律、掌握群友梗）时，可调用
   lesson 工具（action=record/list/forget）把经验写进成长笔记（lessons.md）。
2. 每次请求 LLM 前，on_llm_request 钩子把最近的笔记注入 system_prompt，
   所以它下次遇到类似场景会自动用上学过的东西。
3. 笔记过时了可以用 lesson(action=forget) 按关键词遗忘，防止旧经验误导。

这层「自主学习」是经验层面的：它不能改自己的代码，但能不断积累
「怎么跟这群人相处」的知识——这正是人格成长最值钱的部分。
"""

from __future__ import annotations

import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))  # .../data
LESSONS_PATH = os.path.join(
    DATA_ROOT, "plugin_data", "astrbot_plugin_self_growth", "lessons.md"
)

DEFAULTS = {
    "enabled": True,
    "inject_max_chars": 900,   # 注入 system_prompt 的笔记上限（字符）
    "max_lines": 200,          # 笔记文件最多保留行数
}

GROWTH_HINT = (
    "\n\n【自我成长笔记】以下是你自己此前总结的经验，遇到类似场景请直接运用：\n"
)

# 群聊里只注入行为技巧；主人偏好、群友、群配置等私密笔记只在私聊注入，
# 否则会把一个群/一个人的事带到另一个群里，造成记忆串味。
GROUP_SAFE_CATEGORIES = ("技巧", "雷点")
PRIVATE_CATEGORIES = ("主人偏好", "群友")
_CATEGORY_RE = re.compile(r"^-\s*\[[^\]]*\]\[([^\]]+)\]")


def _category(line: str) -> str:
    m = _CATEGORY_RE.match(line.strip())
    return m.group(1) if m else ""


def _owner_uids() -> set[str]:
    """主人 = AstrBot 主配置里的 admins_id。"""
    try:
        with open(os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return {str(x).strip() for x in (cfg.get("admins_id") or []) if str(x).strip()}
    except Exception:  # noqa: BLE001
        return set()


@register(
    "astrbot_plugin_self_growth",
    "dafeiyu-local",
    "自我成长系统：经验笔记自动注入对话",
    "v1.0.0",
)
class SelfGrowth(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULTS)
        self._ensure_file()
        n = self._line_count()
        logger.info(f"[self_growth] 已启动：成长笔记 {n} 条 ({LESSONS_PATH})")

    # ---------- 文件读写 ----------

    def _ensure_file(self):
        os.makedirs(os.path.dirname(LESSONS_PATH), exist_ok=True)
        if not os.path.exists(LESSONS_PATH):
            with open(LESSONS_PATH, "w", encoding="utf-8") as f:
                f.write("# 大肥鱼的成长笔记\n")

    def _read_lines(self) -> list[str]:
        try:
            with open(LESSONS_PATH, encoding="utf-8") as f:
                return [l.rstrip("\n") for l in f.readlines()]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[self_growth] 读取笔记失败: {e}")
            return []

    def _line_count(self) -> int:
        return sum(1 for l in self._read_lines() if l.strip() and not l.startswith("#"))

    def _append(self, line: str):
        lines = self._read_lines()
        lines.append(line)
        # 超限时保留标题 + 最新记录
        if len(lines) > int(self.cfg["max_lines"]):
            lines = [l for l in lines if l.startswith("#")] + lines[-int(self.cfg["max_lines"]) :]
        with open(LESSONS_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ---------- LLM 请求注入 ----------

    @filter.on_llm_request()
    async def inject_lessons(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg["enabled"]:
            return
        try:
            lines = [
                l for l in self._read_lines()
                if l.strip() and not l.startswith("#")
            ]
            try:
                in_group = bool(event.get_group_id())
            except Exception:  # noqa: BLE001
                in_group = False
            if in_group:
                lines = [l for l in lines if _category(l) in GROUP_SAFE_CATEGORIES]
            else:
                try:
                    sender = str(event.get_sender_id() or "").strip()
                except Exception:  # noqa: BLE001
                    sender = ""
                if sender not in _owner_uids():
                    lines = [l for l in lines if _category(l) not in PRIVATE_CATEGORIES]
            if not lines:
                return
            recent = "\n".join(lines)[-int(self.cfg["inject_max_chars"]) :]
            req.system_prompt += (
                GROWTH_HINT + recent
                + "\n\n（若本次互动让你学到新的教训或规律，可在回复前调用 "
                  "lesson(action=record, content=...) 记下来；发现某条笔记已过时，"
                  "调用 lesson(action=forget, keyword=...) 忘掉它。）"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[self_growth] 注入失败: {type(e).__name__}: {e}")

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="lesson")
    async def lesson(self, event: AstrMessageEvent, action: str, content: str = "",
                     category: str = "经验", keyword: str = ""):
        """管理你的成长笔记（学到的经验）。action：record 记一条 / list 查看全部 / forget 按关键词遗忘。

        用法：
        - action=record：被用户纠正、发现群友喜好或雷点、总结出聊天技巧、意识到做法不当时，
          把经验写进 content，要具体可执行、别写空话；category 可选 经验/群友/雷点/技巧/主人偏好
        - action=list：想回顾自己学过什么、或不确定某条是否记过时
        - action=forget：某条旧经验过时或错误，用 keyword 描述它，删掉相关笔记

        Args:
            action(string): record、list、forget 之一
            content(string): record 时要记的经验内容（一句话）
            category(string): record 时的分类，默认「经验」
            keyword(string): forget 时的关键词，匹配笔记内容
        """
        act = (action or "").strip().lower()

        if act in ("record", "add", "write"):
            text = (content or "").strip().replace("\n", " ")
            if not text:
                return "笔记内容为空，未记录"
            cat = (category or "经验").strip()[:8]
            stamp = time.strftime("%m-%d %H:%M")
            self._append(f"- [{stamp}][{cat}] {text}")
            logger.info(f"[self_growth] 新笔记({cat}): {text[:60]}")
            return f"已记入成长笔记：{text[:50]}"

        if act in ("list", "show", "view"):
            lines = [l for l in self._read_lines() if l.strip() and not l.startswith("#")]
            if not lines:
                return "成长笔记还是空的，一切都等你去学。"
            tail = lines[-40:]
            return f"共 {len(lines)} 条，最近 {len(tail)} 条：\n" + "\n".join(tail)

        if act in ("forget", "delete", "remove"):
            kw = (keyword or "").strip()
            if not kw:
                return "请告诉我要遗忘的关键词"
            lines = self._read_lines()
            kept, removed = [], 0
            for l in lines:
                if (not l.startswith("#")) and kw in l:
                    removed += 1
                else:
                    kept.append(l)
            if removed:
                with open(LESSONS_PATH, "w", encoding="utf-8") as f:
                    f.write("\n".join(kept) + "\n")
            return f"已遗忘 {removed} 条含「{kw}」的笔记"

        return f"不认识的 action：{action}（用 record/list/forget）"
