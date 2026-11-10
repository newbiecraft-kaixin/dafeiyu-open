"""DeepSeek 余额查询、充值提示与 LLM 报错静默。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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


def recharge_link(cfg: dict) -> str:
    """余额/充值展示用的链接：优先自建现抓码页，配置坏了就退回官方充值页。

    历史坑：`recharge_link_base` 被填成「自建域名:52199」（没 scheme，QQ 里点不开）
    或复制成了视频页 `.../bili`，`/余额` 会原样把错的链接发出去。这里统一兜底：
    - 缺 scheme 的补 https://
    - example.com 占位、空值、视频页路径一律视为没配
    """
    def _usable(url: str) -> bool:
        low = url.lower()
        return bool(url) and "example.com" not in low and "/bili" not in low

    base = str(cfg.get("recharge_link_base") or "").strip()
    if base and "://" not in base:
        base = "https://" + base.lstrip("/")
    if _usable(base):
        return base
    official = str(cfg.get("recharge_url") or "").strip()
    return official if _usable(official) else "https://platform.deepseek.com/top_up"


def pick_deepseek_source(cfg: dict) -> tuple[str, str]:
    """从 provider_sources 里挑 DeepSeek 官方 key，返回 (api_base, key)。

    不能只认 id/provider 恰好等于 "deepseek"：AstrBot 里 `provider` 是**类型**
    （通常是 openai_chat_completion），id 又常被写成 deepseek-chat / deepseek-v3，
    按名字精确匹配就会「明明配了 key 却说没配」。所以名字像 deepseek 都算，
    再用 api_base 含 deepseek.com 的优先。
    """
    best: tuple[int, str, str] | None = None
    for src in cfg.get("provider_sources") or []:
        if not isinstance(src, dict):
            continue
        keys = [str(k).strip() for k in (src.get("key") or []) if str(k).strip()]
        if not keys:
            continue
        sid = str(src.get("id") or "").lower()
        sname = str(src.get("provider") or "").lower()
        base = str(src.get("api_base") or "")
        official = "deepseek.com" in base.lower()
        named = sid.startswith("deepseek") or sname.startswith("deepseek")
        if not (official or named):
            continue
        score = 2 if official else 1
        if best is None or score > best[0]:
            best = (score, base or "https://api.deepseek.com/v1", keys[0])
    if best is None:
        return "", ""
    return best[1], best[2]


DEEPSEEK_PLATFORM = "https://platform.deepseek.com"
# platform 的 WAF 认浏览器 UA，缺了会直接返回 429「Request Blocked」
DEEPSEEK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def pick_dsh_account(home: str = "") -> tuple[str, str]:
    """从 DSH 凭据里取 DeepSeek 账号 token，返回 (origin, token)。

    DSH 的 `deepseek-account` 路线没有 API key，凭据存在
    `~/.dsh/.credentials.yaml` 的 `deepseek-account-platform/default` 条目里。
    不引入 PyYAML：按缩进切出这一段，只取 token / issuer。
    """
    base = home or os.environ.get("DSH_HOME") or os.path.join(
        os.path.expanduser("~"), ".dsh"
    )
    try:
        with open(os.path.join(base, ".credentials.yaml"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", ""
    m = re.search(r"(?m)^  deepseek-account-platform/default:\s*$", text)
    if not m:
        return "", ""
    block = re.split(r"(?m)^  \S", text[m.end():], maxsplit=1)[0]
    tok = re.search(r"(?m)^\s+token:\s*(\S+)\s*$", block)
    if not tok:
        return "", ""
    org = re.search(r"(?m)^\s+issuer:\s*(\S+)\s*$", block)
    return (org.group(1) if org else DEEPSEEK_PLATFORM).rstrip("/"), tok.group(1)


def _wallet_sum(wallets, currency: str) -> Decimal:
    total = Decimal("0")
    for w in wallets if isinstance(wallets, list) else []:
        if not isinstance(w, dict):
            continue
        if str(w.get("currency") or "CNY").upper() != currency:
            continue
        try:
            total += Decimal(str(w.get("balance") or "0"))
        except (InvalidOperation, ValueError):
            continue
    return total


class BalanceMixin:

    async def _deepseek_balance_info(self) -> dict | None:
        """查 DeepSeek 官方余额接口，返回第一条币种信息（total_balance / is_available）"""
        self._balance_error = ""
        base, key = "", ""
        try:
            with open(
                os.path.join(DATA_ROOT, "cmd_config.json"), encoding="utf-8-sig"
            ) as f:
                cfg = json.load(f)
            base, key = pick_deepseek_source(cfg)
        except Exception as e:  # noqa: BLE001
            self._balance_error = f"读 cmd_config.json 失败：{type(e).__name__}: {e}"
            logger.warning(f"[human_sim] 读 DeepSeek 配置失败: {type(e).__name__}: {e}")
        if not key:
            # 只登了 DSH 账号、没配 API key 的部署：余额从 DSH 账号查
            cur = await self._dsh_account_balance()
            if cur is not None:
                return cur
            self._balance_error = (
                self._balance_error
                or "provider_sources 里没找到 DeepSeek key（id 不是 deepseek*，api_base 也不含 deepseek.com），DSH 账号也没登录"
            )
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
                    if r.status == 401:
                        self._balance_error = "key 无效（401），去 platform.deepseek.com 重新生成"
                        return None
                    if r.status != 200:
                        self._balance_error = f"接口返回 HTTP {r.status}（{url}）"
                        return None
                    data = await r.json()
        except asyncio.TimeoutError:
            self._balance_error = f"请求超时（{url}）"
            return None
        except Exception as e:  # noqa: BLE001
            self._balance_error = f"请求失败：{type(e).__name__}: {e}"
            logger.warning(f"[human_sim] 查额度失败: {type(e).__name__}: {e}")
            return None
        infos = data.get("balance_infos") or []
        if not infos:
            self._balance_error = "接口没返回余额信息"
            return None
        cur = dict(infos[0])
        cur["is_available"] = bool(data.get("is_available", True))
        return cur

    async def _dsh_account_balance(self) -> dict | None:
        """查 DSH 账号（deepseek-account）余额，字段对齐 DeepSeek 官方接口的返回。

        DSH 走的是平台账号而不是 API key：GET platform.deepseek.com
        /api/v0/users/get_user_summary，鉴权头 x-dsh-auth-token。
        """
        origin, token = pick_dsh_account()
        if not token:
            self._balance_error = (
                "没找到 DSH 账号凭据（~/.dsh/.credentials.yaml 里没有 "
                "deepseek-account-platform 登录记录）"
            )
            return None
        url = origin + "/api/v0/users/get_user_summary"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as sess:
                async with sess.get(
                    url,
                    headers={"x-dsh-auth-token": token, "user-agent": DEEPSEEK_UA},
                ) as r:
                    if r.status == 401:
                        self._balance_error = "DSH 账号凭据过期（401），在 DSH 里重新登录"
                        return None
                    if r.status == 429:
                        self._balance_error = "平台限流（429），过会儿再试"
                        return None
                    if r.status != 200:
                        self._balance_error = f"DSH 账号余额接口返回 HTTP {r.status}"
                        return None
                    body = await r.json()
        except asyncio.TimeoutError:
            self._balance_error = f"请求超时（{url}）"
            return None
        except Exception as e:  # noqa: BLE001
            self._balance_error = f"请求失败：{type(e).__name__}: {e}"
            logger.warning(f"[human_sim] 查 DSH 账号余额失败: {type(e).__name__}: {e}")
            return None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or data.get("biz_code") not in (0, None):
            self._balance_error = "DSH 账号余额接口业务错误（可能未登录或凭据失效）"
            return None
        biz = data.get("biz_data") or {}
        normal = biz.get("normal_wallets") or []
        bonus = biz.get("bonus_wallets") or []
        currency = next(
            (
                str(w.get("currency") or "CNY").upper()
                for w in list(normal) + list(bonus)
                if isinstance(w, dict)
            ),
            "CNY",
        )
        topped = _wallet_sum(normal, currency)
        granted = _wallet_sum(bonus, currency)
        return {
            "currency": currency,
            "total_balance": f"{topped + granted:.2f}",
            "topped_up_balance": f"{topped:.2f}",
            "granted_balance": f"{granted:.2f}",
            "is_available": True,
        }

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
        """上游 agent runner 兼容层：补齐被跳过的消息装配 + 错误文案静默。"""
        try:
            from astrbot.core.agent.runners import tool_loop_agent_runner as _tl

            if getattr(_tl, "_human_sim_quiet_llm_err", False):
                return
            orig_step = _tl.ToolLoopAgentRunner.step
            try:
                from astrbot.core.agent.response import AgentStats as _AgentStats
            except Exception:  # noqa: BLE001
                _AgentStats = None

            async def _quiet_step(runner):
                # 上游 4.29.3 隐患：setup() 在 func_tool 为空时提前 return，跳过了
                # self.stats 初始化；dsh 旁路档位恰好把工具摘光，空消息路径一访问
                # self.stats 就 AttributeError，报错文案还会发进群。这里补上兜底。
                if _AgentStats is not None and not hasattr(runner, "stats"):
                    runner.stats = _AgentStats()
                    runner.stats.start_time = time.time()
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

            try:
                from astrbot.core.agent.message import (
                    Message as _CoreMessage,
                    bind_checkpoint_messages as _bind_msgs,
                )
            except Exception:  # noqa: BLE001
                _CoreMessage = _bind_msgs = None

            orig_reset = _tl.ToolLoopAgentRunner.reset

            async def _fixed_reset(runner, *args, **kwargs):
                # 上游 4.29.3 缺陷：reset() 在 skills_like 模式下 func_tool 为空时
                # 提前 return，把消息装配一并跳过 —— dsh 档位恰好摘光工具，@ 的
                # prompt 根本进不了 LLM（表现为：报错或沉默，永远不回复）。
                # 这里在原 reset 之后补跑被跳过的消息装配，仅对消息为空的会话生效。
                await orig_reset(runner, *args, **kwargs)
                if _CoreMessage is None or _bind_msgs is None:
                    return
                try:
                    rc = getattr(runner, "run_context", None)
                    req = getattr(runner, "req", None)
                    if rc is None or req is None or getattr(rc, "messages", None):
                        return
                    has_content = bool(
                        req.prompt is not None
                        or req.image_urls
                        or req.audio_urls
                        or req.extra_user_content_parts
                    )
                    if not has_content and not req.contexts:
                        return
                    messages = _bind_msgs(list(req.contexts or []))
                    if has_content:
                        m = await runner._assemble_request_context_for_provider(req)
                        messages.append(_CoreMessage.model_validate(m))
                    if req.system_prompt:
                        messages.insert(
                            0, _CoreMessage(role="system", content=req.system_prompt)
                        )
                    rc.messages = messages
                    logger.warning(
                        "[human_sim] 已补齐空工具会话的消息装配（上游 skills_like 提前 return 缺陷）"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[human_sim] 消息装配兜底失败: {type(e).__name__}: {e}"
                    )

            _tl.ToolLoopAgentRunner.reset = _fixed_reset
            _tl._human_sim_quiet_llm_err = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[human_sim] 静默 LLM 错误文案失败: {e}")

    def _deepseek_session(self, event, umo: str = "") -> bool:
        """当前会话是不是走 DeepSeek（官方 key 或 DSH 账号），余额/充值只在这种会话里有意义"""
        umo = umo or event.unified_msg_origin
        pid, pbase, ptype = "", "", ""
        try:
            provider = self.context.get_using_provider(umo)
            pcfg = getattr(provider, "provider_config", {}) or {}
            pid = str(pcfg.get("id") or "")
            pbase = str(pcfg.get("api_base") or "")
            ptype = str(pcfg.get("type") or pcfg.get("provider_source_id") or "")
        except Exception:  # noqa: BLE001
            pass
        # id 可能是 deepseek / deepseek-chat / deepseek-v3；实在没对上就看接口域名
        if pid.lower().startswith("deepseek") or "deepseek.com" in pbase.lower():
            return True
        # DSH（dsh/dsh，桥接 18081）烧的也是 DeepSeek 账号余额
        if (
            pid.lower().startswith("dsh")
            or ptype.lower().startswith("dsh")
            or ":18081" in pbase
        ):
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
            if gid:
                if gid in [str(x) for x in (rc.get("deepseek_group_ids") or [])]:
                    return True
                scene_ids = [rc.get("group_provider_id") or ""] + [
                    str(x) for x in (rc.get("group_provider_chain") or [])
                ]
            else:
                scene_ids = [rc.get("private_provider_id") or ""]
            # 路由插件是按消息时序切 provider 的，/余额 作为命令可能先到、还没切，
            # 所以这里以「该场景配置的 provider」为准；非管理员仍按原逻辑静默。
            if not self._is_manager(event):
                return False
            return any(str(p).startswith(("deepseek", "dsh")) for p in scene_ids if p)
        except Exception:  # noqa: BLE001
            return False

    def _recharge_allowed(self, event) -> bool:
        """余额/充值只对走 DeepSeek（官方 key 或 DSH 账号）的会话开放；本地 ollama 不烧钱，一律不理。
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
        url = recharge_link(self.cfg)
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
