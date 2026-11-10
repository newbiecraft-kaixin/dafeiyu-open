# -*- coding: utf-8 -*-
"""本机工具：给 AI 开放「部分工作权限」。

安全边界：
- 默认仅主人（owner_id）可用，别人调用直接拒绝
- 主人默认全盘可读写（full_disk_access），且可执行本机命令（shell_enabled）
- 非主人（allow_others=true 时）仍锁死在 workspace 沙箱内，也无命令执行
- 读有 6000 字上限，写有 20000 字上限，防撑爆内存/磁盘

v1.2.0：只留 local_exec / local_file 两个工具（计算类交给模型直接算），减少工具描述开销。
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.dirname(os.path.dirname(PLUGIN_DIR))   # .../data
WORKSPACE = os.path.join(DATA_DIR, "local_tools", "workspace")

DEFAULT_CONFIG = {
    "owner_ids": [],  # 主人账号：QQ/微信都算；留空 = 谁都不行（运行时配置文件里填）
    "allow_others": False,      # True = 群友也能用（不建议）
    "full_disk_access": True,   # 主人可读写整台机器的任意路径（False = 只限 workspace 沙箱）
    "shell_enabled": True,     # 主人可执行本机命令（PowerShell）
    "shell_timeout_sec": 60,
    "shell_max_output": 8000,
    "read_max_chars": 6000,
    "write_max_chars": 20000,
}


def _safe_join(rel: str, full: bool = False) -> str | None:
    """路径解析：full=True 时主人可访问整机任意路径；否则锁进 workspace 沙箱。"""
    rel = (rel or ".").strip().replace("\\", "/")
    if full:
        p = os.path.expanduser(rel or ".")
        if not os.path.isabs(p):
            p = os.path.join(WORKSPACE, p)
        return os.path.normpath(p)
    if rel in ("", "."):
        return WORKSPACE
    if rel.startswith(("/", "~")) or ":" in rel.split("/")[0]:
        return None
    cand = os.path.normpath(os.path.join(WORKSPACE, rel))
    try:
        cand = os.path.realpath(cand)
    except Exception:
        return None
    if cand != WORKSPACE and not cand.startswith(WORKSPACE + os.sep):
        return None
    return cand




@register("astrbot_plugin_local_tools", "dafeiyu-local", "本机工具（主人专用沙箱）", "v1.2.0")
class LocalTools(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = dict(DEFAULT_CONFIG)
        cfg_path = os.path.join(PLUGIN_DIR, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8-sig") as f:
                    self.cfg.update(json.load(f))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[local_tools] 读取 config.json 失败: {e}")
        os.makedirs(WORKSPACE, exist_ok=True)
        logger.info(f"[local_tools] 已启动，沙箱={WORKSPACE}，主人={self._owners()}")

    # ---------- 权限 ----------

    def _owners(self) -> set[str]:
        raw = self.cfg.get("owner_ids")
        if isinstance(raw, str):
            raw = [raw]
        return {str(x).strip() for x in (raw or []) if str(x).strip()}

    def _allowed(self, event: AstrMessageEvent) -> bool:
        # 本机权限只认「主人私聊」：群里一律不给，避免群友借机引用执行
        if (
            not str(event.get_group_id() or "")
            and str(event.get_sender_id() or "") in self._owners()
        ):
            return True
        return bool(self.cfg.get("allow_others"))

    def _full_disk(self) -> bool:
        return bool(self.cfg.get("full_disk_access", True))

    # ---------- 文件 ----------

    def _do_list(self, sub_path: str) -> str:
        base = _safe_join(sub_path, self._full_disk())
        if not base or not os.path.isdir(base):
            return f"目录不存在或越界：{sub_path}"
        rows = []
        try:
            for n in sorted(os.listdir(base))[:100]:
                fp = os.path.join(base, n)
                if os.path.isdir(fp):
                    rows.append(f"[目录] {n}/")
                else:
                    st = os.stat(fp)
                    rows.append(f"{n}  {st.st_size}B  {time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))}")
        except OSError as e:
            return f"读取失败：{e}"
        shown = base if self._full_disk() else "工作区/" + os.path.relpath(base, WORKSPACE).replace("\\", "/")
        head = f"{shown} 共 {len(rows)} 项：\n" if rows else f"{shown} 是空目录"
        return head + "\n".join(rows)

    def _do_read(self, sub_path: str) -> str:
        fp = _safe_join(sub_path, self._full_disk())
        if not fp or not os.path.isfile(fp):
            return f"文件不存在或越界：{sub_path}"
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return f"读取失败：{e}"
        cap = int(self.cfg.get("read_max_chars", 6000))
        head = text[:cap]
        more = f"\n…（共 {len(text)} 字，已截断）" if len(text) > cap else ""
        if not head.strip():
            return f"《{sub_path}》是空文件"
        return f"《{sub_path}》内容：\n{head}{more}"

    def _do_write(self, sub_path: str, content: str) -> str:
        fp = _safe_join(sub_path, self._full_disk())
        if not fp:
            return f"路径越界：{sub_path}"
        content = str(content)
        if len(content) > int(self.cfg.get("write_max_chars", 20000)):
            return f"内容太长（{len(content)} 字 > 20000），拒绝写入"
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            return f"写入失败：{e}"
        return f"已写入 {fp}（{len(content)} 字）"

    def _do_append(self, content: str) -> str:
        line = f"- {time.strftime('%Y-%m-%d %H:%M')} {str(content)[:500]}\n"
        try:
            with open(os.path.join(WORKSPACE, "记事本.md"), "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            return f"记事失败：{e}"
        return "已记到 记事本.md。"

    @filter.llm_tool(name="local_file")
    async def local_file(self, event: AstrMessageEvent, action: str, path: str = ".", content: str = ""):
        """读写本机文件（仅主人）。action 取值：list 列目录 / read 读文本 / write 覆盖写 / append 追到记事本。

        Args:
            action(string): list、read、write、append 之一
            path(string): 文件或目录路径（相对工作区或绝对路径；append 时忽略）
            content(string): write 时写入的完整内容；append 时要记的内容
        """
        if not self._allowed(event):
            return "这个工具只有主人能用。"
        act = (action or "").strip().lower()
        if act in ("list", "ls", "dir"):
            return self._do_list(path or ".")
        if act in ("read", "cat", "open"):
            return self._do_read(path)
        if act in ("write", "save"):
            return self._do_write(path, content)
        if act in ("append", "note"):
            return self._do_append(content)
        return f"不认识的 action：{action}（用 list/read/write/append）"

    # ---------- 系统 ----------

    def _do_shell(self, command: str) -> str:
        if not self.cfg.get("shell_enabled", True):
            return "本机命令执行已关闭。"
        cmd = str(command or "").strip()
        if not cmd:
            return "命令为空。"
        limit = int(self.cfg.get("shell_timeout_sec", 60) or 60)
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                cwd=WORKSPACE,
                capture_output=True,
                timeout=limit,
            )
        except subprocess.TimeoutExpired:
            return f"命令超时（{limit}s）。"
        except OSError as e:
            return f"执行失败：{e}"
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        cap = int(self.cfg.get("shell_max_output", 8000))
        if len(out) > cap:
            out = out[:cap] + f"\n…（输出已截断，共 {len(out)} 字）"
        body = out or "(无输出)"
        if err:
            body += "\n[stderr]\n" + err[:2000]
        return f"exit={proc.returncode}\n{body}"

    async def _do_screenshot(self, event: AstrMessageEvent) -> str:
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            fp = os.path.join(WORKSPACE, f"截屏_{time.strftime('%Y%m%d_%H%M%S')}.png")
            img.save(fp)
        except Exception as e:  # noqa: BLE001
            return f"截屏失败：{type(e).__name__}: {e}"
        try:
            await event.send(MessageChain(chain=[Image.fromFileSystem(fp)]))
        except Exception as e:  # noqa: BLE001
            return f"截屏成功但发送失败：{type(e).__name__}: {e}（文件在 {fp}）"
        return "截图已发送。"

    @filter.llm_tool(name="local_exec")
    async def local_exec(self, event: AstrMessageEvent, action: str, arg: str = ""):
        """本机操作（仅主人）。action：shell 执行命令（arg=命令）/ screenshot 截屏发给你。

        Args:
            action(string): shell、screenshot 之一
            arg(string): shell 时要执行的 PowerShell 命令，其它留空
        """
        if not self._allowed(event):
            return "这个工具只有主人能用。"
        act = (action or "").strip().lower()
        if act in ("shell", "cmd", "run"):
            return self._do_shell(arg)
        if act in ("screenshot", "shot", "screen"):
            return await self._do_screenshot(event)
        return f"不认识的 action：{action}（用 shell/screenshot）"

    async def terminate(self):
        pass
