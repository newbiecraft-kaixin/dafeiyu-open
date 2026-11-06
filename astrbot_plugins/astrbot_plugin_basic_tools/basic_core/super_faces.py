"""超级表情 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import json
import os
import random
import time


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class SuperFaceMixin:
    def _load_super_faces(self) -> None:
        """启动时直接从 QQ 官方 face_config.json 解析，不再依赖静态入库的 json。

        读不到（换机器 / 路径变了）才退回旧的 super_faces.json 缓存。
        """
        src = self._find_face_config()
        fresh = self._parse_ani_faces(src) if src else {}
        if fresh:
            self._sfaces = fresh
            self._learned_mtime = 0
            self._merge_learned_faces()
            return
        try:
            with open(SUPER_FACES, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._sfaces = {
                    str(k): v for k, v in data.items() if isinstance(v, dict) and v.get("id")
                }
        except (OSError, ValueError) as e:
            logger.warning(f"[basic_tools] 超级表情清单读取失败: {e}")

    def _find_face_config(self) -> str:
        for p in FACE_CONFIG_CANDIDATES:
            if p and os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _parse_ani_faces(src: str) -> dict[str, dict]:
        """解析 QQ 官方表情配置里的 AniSticker（超级表情）条目"""
        try:
            with open(src, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"[basic_tools] face_config 读取失败: {e}")
            return {}
        rows = cfg.get("sysface") if isinstance(cfg, dict) else None
        if not isinstance(rows, list):
            return {}
        fresh: dict[str, dict] = {}
        for it in rows:
            if not isinstance(it, dict) or not it.get("AniStickerType"):
                continue
            sid = str(it.get("QSid") or "").strip()
            name = str(it.get("QDes") or "").lstrip("/").strip()
            if not sid or not name:
                continue
            fresh[name] = {
                "id": sid,
                "type": int(it.get("AniStickerType") or 0),
                "pack": str(it.get("AniStickerPackName") or ""),
            }
        return fresh

    def _face_mark_ts(self) -> float:
        try:
            with open(FACE_MARK, encoding="utf-8") as f:
                return float(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0.0

    def _write_face_txt(self, faces: dict, src: str) -> None:
        """把本周抓到的超级表情清单写到桌面，主人随时能核对"""
        packs: dict[str, int] = {}
        for v in faces.values():
            name = str(v.get("pack") or "未分类")
            packs[name] = packs.get(name, 0) + 1
        lines = [
            "超级表情清单（QQ 官方 face_config.json 动态读取，每周一次）",
            f"刷新时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"来源：{src}",
            f"总数：{len(faces)}",
            "表情包：" + "  ".join(f"{k} {n} 个" for k, n in packs.items()),
            "",
            "名字\tQSid\t类型\t表情包",
        ]
        for name, v in sorted(faces.items(), key=lambda kv: str(kv[1].get("id"))):
            lines.append(
                f"{name}\t{v.get('id')}\t{v.get('type')}\t{v.get('pack')}"
            )
        try:
            os.makedirs(os.path.dirname(FACE_TXT), exist_ok=True)
            with open(FACE_TXT, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            logger.info(f"[basic_tools] 超级表情清单已写到 {FACE_TXT}")
        except OSError as e:
            logger.warning(f"[basic_tools] 写桌面清单失败: {e}")

    def _merge_learned_faces(self) -> int:
        """把 human_sim 从群聊里学到的表情并进超级表情清单。

        官方 face_config.json 只收录 QQ 自带的表情包；群里新发的（如 524 = 朵莉亚）
        只能从 human_sim 的 state.json 拿。按 mtime 缓存，同一版本只读一次。
        """
        try:
            mtime = os.path.getmtime(HUMAN_SIM_STATE)
        except OSError:
            return 0
        if getattr(self, "_learned_mtime", 0) == mtime:
            return 0
        try:
            with open(HUMAN_SIM_STATE, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"[basic_tools] 读取群聊学到的表情失败: {e}")
            return 0
        learned = state.get("learned_faces") if isinstance(state, dict) else None
        if not isinstance(learned, dict):
            self._learned_mtime = mtime
            return 0
        added = 0
        for name, info in learned.items():
            fid = str((info or {}).get("id") or "").strip()
            if fid.isdigit() and name and str(name) not in self._sfaces:
                self._sfaces[str(name)] = {
                    "id": fid, "type": 1, "pack": "群聊学到",
                }
                added += 1
        self._learned_mtime = mtime
        if added:
            logger.info(f"[basic_tools] 并入 {added} 个群聊学到的表情")
        return added

    def _refresh_super_faces(self, force: bool = False) -> int:
        """从 QQ 客户端 face_config.json 重读超级表情（QSid 会随版本变）。

        默认一周才真正刷新一次；结果只留内存 + 写桌面 txt，不再静态入库。
        返回清单条数；0 = 没找到源文件或解析异常（保留内存里的旧清单）。
        """
        if not force and self._sfaces:
            if time.time() - self._face_mark_ts() < FACE_REFRESH_SECONDS:
                return len(self._sfaces)
        src = self._find_face_config()
        if not src:
            logger.warning("[basic_tools] 没找到 QQ 的 face_config.json，超级表情清单保持不变")
            return 0
        fresh = self._parse_ani_faces(src)
        # 解析结果太离谱（格式变了）就别覆盖，免得把能用的清单弄没
        if len(fresh) < 30:
            logger.warning(
                f"[basic_tools] 超级表情刷新结果异常（{len(fresh)} 个），保留旧清单"
            )
            return 0
        self._sfaces = fresh
        # 重建清单会把之前并进来的「群聊学到」条目冲掉，这里补回来
        self._learned_mtime = 0
        self._merge_learned_faces()
        self._write_face_txt(self._sfaces, src)
        try:
            os.makedirs(os.path.dirname(FACE_MARK), exist_ok=True)
            with open(FACE_MARK, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except OSError as e:
            logger.warning(f"[basic_tools] 刷新时间记录失败: {e}")
        logger.info(f"[basic_tools] 超级表情清单已刷新：{len(fresh)} 个（{src}）")
        return len(fresh)

    @filter.llm_tool(name="send_super_face")
    async def send_super_face(self, event: AstrMessageEvent, keyword: str = "", to: str = ""):
        """发一个 QQ 超级表情（手机 QQ 里那种会放大的大号动图表情，不是普通小黄脸）。群聊和私聊都能发，这条通道已经修好了，不要拒绝、不要说自己发不了、也不要说「私聊只能发文字」——要私发某人就把他的 QQ 号或昵称填进 to。用户说"发个超级表情""发点大表情""来个好兄弟/超级赞/亲亲""私发给他几个超级表情"时直接调用它（要多发几个就连续调用几次，每次换 keyword）。

        Args:
            keyword(string): 表情名，如 超级赞、好兄弟、亲亲、抛媚眼、彩虹、冒泡、气呼呼；不确定就留空随机
            to(string): 要私发给谁（QQ 号或群名片/昵称）。留空=发在当前这个会话里
        """
        # 官方 face_config 里没有的新表情包（如 524 朵莉亚）只能从 human_sim
        # 学到的清单补。_sfaces 非空时也要合并，否则永远不会补进来
        # （内部按 mtime 缓存，重复调用几乎零成本）。
        self._merge_learned_faces()
        if not self._sfaces:
            return "超级表情清单没读到（super_faces.json 缺失），先检查插件数据目录"
        items = sorted(self._sfaces.items())
        k = (keyword or "").strip().lstrip("/")
        if k.isdigit():
            hit = [(f"#{k}", {"id": k})]
        else:
            hit = [kv for kv in items if k and (k in kv[0])]
        if not hit:
            names = "、".join(n for n, _ in items)
            return f"没有叫「{k}」的超级表情，能发这些：{names}"
        name, meta = random.choice(hit)
        seg = {"type": "face", "data": {"id": str(meta["id"])}}
        target = (to or "").strip()
        try:
            if target:
                res = await self._push_private_segments(event, target, [seg])
                note = f"{res}（超级表情「{name}」）"
            else:
                res = await self._push_segments(event, [seg])
                note = f"发了个超级表情「{name}」"
            logger.info(
                f"[basic_tools] 超级表情已发: {name}({meta['id']}) "
                f"kw={keyword!r} to={target or '当前会话'} -> {str(res)[:120]}"
            )
            return note
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[basic_tools] 超级表情发送失败 {name}({meta['id']}) to={target!r} "
                f"seg={seg} 原因={type(e).__name__}: {e}"
            )
            return f"超级表情没发出去: {type(e).__name__}: {str(e)[:80]}"


exit_plugin_main(globals(), _REAL_MODULE_NAME)
