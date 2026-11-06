"""DeepSeek 余额查询、充值提示与 LLM 报错静默。"""

from __future__ import annotations

import asyncio
import json
import os
import random
from collections import deque
from datetime import datetime, timedelta, timezone
import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import (
    At,
    Face,
    File,
    Forward,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)

from .config import DATA_ROOT
from .text import BALANCE_ASK_RE, BALANCE_NOISE_RE, _IDENTITY_TAG_RE

# 让本模块内定义的函数/方法 __module__ 仍归属 main：
# AstrBot 依赖 __module__ 做插件归属、实例绑定与热重载清理，拆分后必须保持一致。
if __package__:
    __name__ = __package__.rsplit(".", 1)[0] + ".main"


class BalanceMixin:

    async def _deepseek_balance_info(self) -> dict | None:
        """查 DeepSeek 官方余额接口，返回第一条币种信息（total_balance / is_available）"""
        base, key = "", ""
        try:
            with open(
                os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig"
            ) as f:
                cfg = json.load(f)
            for src in cfg.get("provider_sources") or []:
                if str(src.get("id")) == "deepseek" or str(
                    src.get("provider")
                ) == "deepseek":
                    keys = src.get("key") or []
                    if keys:
                        base = str(src.get("api_base") or "https://api.deepseek.com/v1")
                        key = str(keys[0])
                        break
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 读 DeepSeek 配置失败: {type(e).__name__}: {e}")
        if not key:
            return None
        root = base.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        url = root.rstrip("/") + "/user/balance"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as sess:
                async with sess.get(
                    url, headers={"Authorization": f"Bearer {key}"}
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 查额度失败: {type(e).__name__}: {e}")
            return None
        infos = data.get("balance_infos") or []
        if not infos:
            return None
        cur = dict(infos[0])
        cur["is_available"] = bool(data.get("is_available", True))
        return cur

    async def _deepseek_balance_line(self) -> str:
        """把余额信息拼成一句人话"""
        cur = await self._deepseek_balance_info()
        if not cur:
            return ""
        money = cur.get("total_balance")
        if not cur.get("is_available", True):
            return f"没了，额度用光了（还剩 {money}），得找主人充值"
        return random.choice(
            (
                f"还剩 {money} 块钱额度，省着点烧",
                f"查了下，余额 {money}，还够我唠一阵",
                f"额度还剩 {money}，暂时饿不死",
            )
        )

    def _silence_llm_error_chat(self) -> None:
        """兜底取消后，模型失败只写日志，错误文案不再发到群里（失败就当没听见）"""
        try:
            from astrbot.core.agent.runners import tool_loop_agent_runner as _tl

            if getattr(_tl, "_human_sim_quiet_llm_err", False):
                return
            orig_step = _tl.ToolLoopAgentRunner.step

            async def _quiet_step(runner):
                async for resp in orig_step(runner):
                    if getattr(resp, "type", "") == "err":
                        data = getattr(resp, "data", None)
                        chain = (
                            data.get("chain")
                            if isinstance(data, dict)
                            else getattr(data, "chain", None)
                        )
                        text = ""
                        if chain is not None:
                            try:
                                text = chain.get_plain_text()
                            except Exception:  # noqa: BLE001
                                text = str(chain)
                        logger.warning(
                            f"[human_sim] LLM 调用失败，错误文案已静默（日志保留）: {text}"
                        )
                        continue
                    yield resp

            _tl.ToolLoopAgentRunner.step = _quiet_step
            _tl._human_sim_quiet_llm_err = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 静默 LLM 错误文案失败: {e}")

    def _deepseek_session(self, event, umo: str = "") -> bool:
        """当前会话是不是走 DeepSeek 官方 key（余额/充值只在这种会话里有意义）"""
        umo = umo or event.unified_msg_origin
        try:
            provider = self.context.get_using_provider(umo)
            pid = str((getattr(provider, "provider_config", {}) or {}).get("id") or "")
        except Exception:  # noqa: BLE001
            pid = ""
        if pid.startswith("deepseek"):
            return True
        # provider_router 的名单群是消息进来才切 provider，命令可能先到，直接看它的配置
        try:
            with open(
                os.path.join(
                    DATA_ROOT, "plugins", "astrbot_plugin_provider_router", "config.json"
                ),
                encoding="utf-8-sig",
            ) as f:
                rc = json.load(f)
            gid = str(getattr(event, "get_group_id", lambda: "")() or "")
            return gid in [str(x) for x in (rc.get("deepseek_group_ids") or [])]
        except Exception:  # noqa: BLE001
            return False

    def _recharge_allowed(self, event) -> bool:
        """余额/充值只对走 DeepSeek 官方 key 的会话开放；本地 ollama 不烧钱，一律不理。
        recharge_owner_only 打开后，再缩到只认主人。"""
        if not self._deepseek_session(event):
            return False
        if not self.cfg.get("recharge_owner_only", False):
            return True
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender = ""
        owner = str(self.cfg.get("owner_id") or "")
        return bool(sender and owner and sender == owner)

    async def _notify_balance_empty(self, umo: str) -> None:
        """余额不足时只在这个群被动提示一次（有人找我但回不了话的时候）"""
        if not umo or not self.cfg.get("balance_empty_notice_enabled", True):
            return
        # 只在自己管得着的群里提示，别在别人的群暴露主人的账户情况
        if umo not in self.cfg["watch_groups"]:
            return
        now = datetime.now().timestamp()
        cd = float(self.cfg.get("balance_empty_notice_cooldown_minutes", 10)) * 60
        if now - float(self.state.get(f"balance_empty_ts:{umo}") or 0) < cd:
            return
        self.state[f"balance_empty_ts:{umo}"] = now
        url = str(
            self.cfg.get("recharge_url") or "https://platform.deepseek.com/top_up"
        )
        line = f"我这边 API 余额不足了，回不了话，先去充一下：{url}"
        try:
            await self._send(umo, [Plain(line)])
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[human_sim] 余额不足提示发送失败: {type(e).__name__}: {e}")
            return
        logger.info(f"[human_sim] 余额不足提示（群 {umo.rsplit(':', 1)[-1]}）")

    async def _handle_balance_request(self, event: AstrMessageEvent) -> bool:
        """群友问「还有多少额度 / 余额」：本地查余额接口，直接回一句，不进 AI。"""
        text = (event.message_str or "").strip()
        text = _IDENTITY_TAG_RE.sub("", text).strip()
        if not (2 <= len(text) <= 14) or not BALANCE_ASK_RE.search(text):
            return False
        if BALANCE_NOISE_RE.search(text) or not self._recharge_allowed(event):
            return False
        line = await self._deepseek_balance_line()
        if not line:
            return False
        umo = event.unified_msg_origin
        try:
            await asyncio.sleep(random.uniform(0.4, 1.0))
            await self._send(umo, [Plain(line)])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 额度回复失败: {type(e).__name__}: {e}")
            return False
        self._history.setdefault(umo, deque(maxlen=30)).append(f"大肥鱼：{line}")
        logger.info(f"[human_sim] 额度查询（群 {umo.rsplit(':', 1)[-1]}）：{line}")
        return True
