"""表情包索引插件：把 LLM 回复里的 :文字名称: 替换成本地表情包图片 / QQ 黄脸。

索引文件格式（UTF-8）：
    文字名称:表情包意思:相对图片路径
    文字名称:相对图片路径            # 兼容旧格式
    文字名称:意思:face:ID           # QQ 官方黄脸

图片路径相对于 pack_dir 解析，绝对路径也可用。

v1.3.0 换了推荐内核（池子对 AI 不可见，只能靠文字对上）：
  1. 推荐只按**文字相似度**：AI 写 :情绪词: / 感受短语 → 跟全库每条的「名字 + 含义」
     算字面分数（IDF 加权二元组余弦 + 整串命中 + 情绪近义词扩展），
     分数带内随机抽 —— 同词每次都能抽到不同的图。
  2. **图片相似度彻底退出推荐**：dHash 只在收藏入库查重时用（阈值 0.90）。
     长得像的图不再互相压制，池子不会被指纹绞窄。
  3. 主库 stickers/ 与收藏库 stickers/fav/ 物理分开，但一起参与文字排名；
     新收的图有新鲜度加成，收进来就能被抽到 —— 库越杂推荐越活。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Face, Image, Plain
from astrbot.api.star import Context, Star, register

from .collect import CollectMixin
from .faces import (
    COMMON_FACES,
    EMOJI_TO_FACE,
    FACE_CATALOG,
    FACE_MOOD_RULES,
    FACE_SUBSTITUTE,
    KAOMOJI_TO_FACE,
    FaceMixin,
)
from .fingerprint import FingerprintMixin
from .followup import FollowupMixin
from .index import IndexMixin
from .llm import LlmMixin
from .quality import QualityMixin
from .ranker import EMOTION_LEXICON, StickerRanker
from .recommend import RecommendMixin
from .render import RenderMixin
from .sending import SendingMixin
from .settings import DEFAULT_CONFIG, PLACEHOLDER, PLUGIN_DIR, send_gate
from .state import StateMixin
from .store import StoreMixin


@register(
    "astrbot_plugin_sticker_pack",
    "dafeiyu-local",
    "表情包索引：占位符渲染 / 推荐补表情 / 收藏查重入库",
    "v1.3.0",
)
class StickerPackPlugin(
    RenderMixin,
    SendingMixin,
    IndexMixin,
    StateMixin,
    RecommendMixin,
    FaceMixin,
    CollectMixin,
    FingerprintMixin,
    StoreMixin,
    QualityMixin,
    FollowupMixin,
    LlmMixin,
    Star,
):
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = dict(DEFAULT_CONFIG)
        cfg_path = os.path.join(PLUGIN_DIR, "config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8-sig") as f:
                    self.config.update(json.load(f))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[sticker_pack] 读取 config.json 失败: {e}")

        self.pack_dir = self.config["pack_dir"]
        self.map_path = os.path.join(self.pack_dir, self.config["map_file"])
        self.stickers: dict[str, dict] = {}
        self.index_text = ""
        self._state_path = os.path.join(PLUGIN_DIR, "_append_state.json")
        self._state: dict = {}
        self._follow_rate: list[float] = []          # 补表情每分钟滑动窗口（内存态）
        self._last_follow: dict[str, float] = {}     # umo -> 上次补表情时间（冷却）
        self._follow_running: set[str] = set()       # 正在跑推荐流程的群（同群互斥，防递归）
        # 延时单发任务：必须持有引用，否则被 GC 回收 → 表情永远发不出去且无任何日志
        self._solo_tasks: set[asyncio.Task] = set()
        self._bots: dict[str, object] = {}           # umo -> bot（后台补表情要带 self_id 路由）
        self._gate = send_gate                       # 出站节流：同群两条消息至少隔 1.6~3 秒
        self._last_group_img: dict[str, dict] = {}   # umo -> {img: Image组件, ts}（供 AI 主动收藏）
        self._self_ids: dict[str, str] = {}          # umo -> self_id
        # 同群最近发过的表情名：真人不会连着几天用同一张，这里做轮换冷却
        self._recent: dict[str, list[str]] = {}
        # 同群最近配过的黄脸名（语义配脸轮换用，避免同一个群老回「呲牙」）
        self._face_recent: dict[str, list[str]] = {}
        # 同群累计使用次数：比「最近窗口」更能压住高频脸，长尾图才有出头机会
        self._use_count: dict[str, dict[str, int]] = {}
        # 上面三张轮换表原来只活到进程结束：每次重启「谁刚发过、发过几次」全清零，
        # 于是同群反复撞上同一张（实测日志里「笑出声」10 小时出现 7 次、
        # 「好吃」5 次）。落盘到 _rotate_state.json，重启后接着轮换。
        self._rotate_path = os.path.join(PLUGIN_DIR, "_rotate_state.json")
        self._rotate_saved_at = 0.0
        self._rotate_dirty = False
        # 图 -> 语义组（同组只出一条候选；图库变动时清空重建）
        self._group_cache: dict[str, str] = {}
        # 同群上次「任何」表情出站时间：单发 / 语义配脸 / 补表情 共用一道闸，
        # 防止一条长回复被拆成三条消息时每条都带一张、看上去像在刷图。
        self._last_emit: dict[str, float] = {}
        # 同群发言密度账本：[(时间, "text"/"emoji")]。只靠「30 秒一次」在活跃群里
        # 等于没闸——群里每分钟都有人说话，表情就会跟着每条刷一张。这里按「每 N 条
        # 文字才允许 1 张表情」算配额，不管群里聊得多快都不会冒出成串的图。
        self._density: dict[str, list[tuple[float, str]]] = {}
        # 全库文字相似度排序器（主库 + 收藏库一起建索引；图库变动时重建）
        self._ranker_obj: StickerRanker | None = None
        self._rank_cache: dict[str, list[tuple[str, float]]] = {}
        self._fallback_cache: dict[str, list[str]] = {}   # 情绪簇 -> 代表图（库变了要清）
        self._all_images_cache: list[str] = []
        # 收藏：dHash 指纹、待确认、缺图需求
        self._dhash_map: dict[str, str] = {}
        self._pending: dict[str, dict] = {}
        self._deficit: list[dict] = []
        # 自主收藏每日额度：{"date": "YYYY-MM-DD", "count": N}，落盘到 _rotate_state.json
        self._collect_quota: dict = {"date": "", "count": 0}
        self._mon_task: asyncio.Task | None = None
        self._hash_task: asyncio.Task | None = None
        self._maint_task: asyncio.Task | None = None
        # 主人名单：主人亲手发的图算「手动收藏」，不占每日自主收藏额度
        self.admins: list[str] = []
        try:
            cfg_path = os.path.abspath(
                os.path.join(PLUGIN_DIR, "..", "..", "cmd_config.json")
            )
            with open(cfg_path, encoding="utf-8-sig") as f:
                self.admins = [
                    str(a).strip() for a in (json.load(f).get("admins_id") or [])
                ]
        except Exception:  # noqa: BLE001
            self.admins = []
        # 补表情流程度量：触发通过/被拦原因/成功发送（监测周期内）
        self._follow_stats: dict[str, int] = {"pass": 0, "sent": 0,
                                              "block_prob": 0, "block_cap": 0,
                                              "block_running": 0, "block_cool": 0,
                                              "block_gap": 0,
                                              "no_pick": 0, "pool_empty": 0,
                                              "send_fail": 0}

    async def initialize(self) -> None:
        self._mon_task = asyncio.create_task(self._monitor_loop())
        # 索引必须在这里加载：原来 _load 被误放在 _monitor_loop 的 while True 之后（死代码），
        # 导致运行时 self.stickers 永远为空——占位符渲染不出、颜文字不转换、补表情全死。
        try:
            with open(self._state_path, encoding="utf-8") as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}
        self._load()
        self._load_rotate()
        fav_n = self._load_fav()
        super_n = self._load_super_faces()
        self._load_pending()
        self._build_index()
        self._build_ranker()
        self._hash_task = asyncio.create_task(self._warm_hashes())
        self._maint_task = asyncio.create_task(self._maintenance_loop())
        logger.info(
            f"[sticker_pack] 推荐式补表情已启用：概率 "
            f"{self.config.get('follow_up_probability', 0.65)}，"
            f"每分钟上限 {self.config.get('follow_up_per_minute', 3)}，"
            f"冷却 {self.config.get('follow_up_cooldown_seconds', 30)}s；"
            f"收藏库 {fav_n} 条；超级表情 {super_n} 个"
        )

    async def terminate(self) -> None:
        for t in (self._mon_task, self._hash_task, self._maint_task):
            if t:
                t.cancel()
        if self._rotate_dirty:
            self._save_rotate(force=True)

    @filter.on_llm_request()
    async def _inject_index(self, event: AstrMessageEvent, req):
        """把「情绪词表」注入 system_prompt（不列图名，池子对 AI 不可见）"""
        if not self.config.get("inject_index") or not self.index_text:
            return
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【表情】想配表情时，在回复里写 :情绪词:（如 :吃瓜:），系统会自动挑一张图单独发出去。"
            "可用：" + self.index_text + "。"
            "也可以写更具体的感受短语（如 :好想睡觉:），一段话最多配一个，别句句配。"
        )

    @filter.on_decorating_result()
    async def _replace_placeholders(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return
        # 第一遍：unicode emoji / 裸关键词 -> :名称: 占位符
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                new_text = self._preconvert(comp.text)
                if new_text != comp.text:
                    try:
                        comp.text = new_text
                    except Exception:  # noqa: BLE001
                        continue
        umo = event.unified_msg_origin
        chain: list = []
        solo: list[dict] = []
        budget = [int(self.config.get("max_stickers_per_message", 1))]
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                segs, got = self._split_stickers(comp.text, umo, budget)
                chain.extend(segs)
                solo.extend(got)
            else:
                chain.append(comp)
        # 空白文本段清掉，免得空气泡
        chain = [
            c for c in chain
            if not (isinstance(c, Plain) and not (c.text or "").strip())
        ]
        # 一条回复最多留一张表情：占位符多的回复会把好几个 Face 内联进来，
        # 发出去就是一条消息挂三四张图，看着很不像人。超出部分直接丢掉。
        _cap = max(0, int(self.config.get("max_stickers_per_message", 1)))
        _seen_media = 0
        _capped: list = []
        for comp in chain:
            if isinstance(comp, (Face, Image)):
                _seen_media += 1
                if _seen_media > _cap:
                    continue
            _capped.append(comp)
        chain = _capped
        # 这条回复有正文 → 先给同群密度记账，后面单发/配脸/补表情都要看这笔账
        if "".join((c.text or "") for c in chain if isinstance(c, Plain)).strip():
            self._note_text(umo)
        # 单发也最多一张，且要过同群间隔+密度闸
        if solo:
            solo = solo[:1] if (self._emit_allowed(umo) and _cap > 0) else []
        if not chain and not solo:
            return
        # 整条只有表情（没文字）：直接内联，本身就是一条纯表情消息
        if not chain and solo:
            chain = [self._to_component(solo.pop(0))]
            self._mark_emit(umo)
        _has_media = any(isinstance(c, (Face, Image)) for c in chain)
        _text_all = "".join((c.text or "") for c in chain if isinstance(c, Plain)).strip()
        # 探针：库外名字被抹掉时留痕，便于判断人设清单与图库是否对得上
        _leftover = [
            m.group(0) for c in chain
            if isinstance(c, Plain) and c.text
            for m in [PLACEHOLDER.search(c.text)] if m
        ]
        if _leftover:
            logger.info(
                f"[sticker_pack] 渲染探针: 残留占位符 {_leftover}"
                f"（库里没有对应条目，索引共 {len(self.stickers)} 条）"
            )
            for c in chain:
                if isinstance(c, Plain) and c.text and PLACEHOLDER.search(c.text):
                    c.text = PLACEHOLDER.sub(" ", c.text).strip()
        result.chain = chain
        # 单独发这一条表情（文字已由主流水线发出，这里延时跟上）
        if solo:
            self._remember_bot(event, umo)
            self._mark_emit(umo)
            task = asyncio.create_task(
                self._send_solo_stickers(
                    umo, solo, random.uniform(*self.config.get("solo_delay_seconds", [0.8, 2.6]))
                )
            )
            self._solo_tasks.add(task)
            task.add_done_callback(self._solo_tasks.discard)
            logger.info(
                f"[sticker_pack] 已排队单发 {len(solo)} 张："
                + "、".join(f"{s['name']}({s.get('kind')})" for s in solo)
                + f" -> {umo.rsplit(':', 1)[-1]}"
            )
            return  # 本条已经配了表情，不再走补表情推荐
        if _text_all and not _has_media:
            # 语义配脸：模型不用写 :情绪词:（那是 AI 腔），系统自己从正文里切词判断情绪，
            # 命中了就在末尾补一张常用脸——正文一个字不动，读起来就是「说完顺手点了个表情」
            if len(_text_all) >= int(self.config.get("auto_face_min_chars", 4)) and random.random() < float(
                self.config.get("auto_face_probability", 0.45)
            ) and self._emit_allowed(umo) and umo.split(":", 1)[0] == "aiocqhttp":
                face = self._pick_semantic_face(_text_all, umo)
                if face:
                    self._mark_face_used(umo, face)
                    self._mark_emit(umo)
                    chain.append(Face(id=self.stickers[face]["value"]))
                    result.chain = chain
                    logger.info(
                        f"[sticker_pack] 语义配脸「{face}」-> {umo.rsplit(':', 1)[-1]}"
                    )
                    return
            self._schedule_followup(event, _text_all)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def _on_private(self, event: AstrMessageEvent):
        """私聊发图：算法自主判断收不收；不看关键词、不追问、不回复。"""
        if not self.config.get("fav_enabled", True):
            return
        images = [c for c in event.get_messages() if isinstance(c, Image)]
        if not images:
            return
        # 收表情是主人特权；别人的图不进库，也不拦消息，交给正常看图
        if not self._is_owner(event.get_sender_id()):
            return
        umo = event.unified_msg_origin
        text = (event.message_str or "").strip()
        try:
            ok, note = await self._collect_if_worthy(
                images[0], umo, event=event, context_text=text,
                review=False, quota=False,
            )
            if ok:
                logger.info(f"[sticker_pack] 私聊自主收藏「{note}」")
            else:
                logger.info(f"[sticker_pack] 私聊图放弃收藏：{note or '不合适'}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 私聊自主收藏失败: {type(e).__name__}: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _on_group_image(self, event: AstrMessageEvent):
        """群图：算法自主判断收不收。

        触发只看「库里缺什么情绪」——有缺图需求才考虑；
        收不收由合规闸门（质量/违规词/视觉粗筛）+ 看图模型认图评分决定，
        不看任何关键词，也不回复、不追问。
        """
        if not self.config.get("fav_enabled", True):
            return
        umo = event.unified_msg_origin
        try:
            imgs = [c for c in event.get_messages() if isinstance(c, Image)]
            if not imgs:
                return
            if self._is_owner(event.get_sender_id()):
                self._last_group_img[umo] = {
                    "img": imgs[0],
                    "ts": time.time(),
                    "sender": str(event.get_sender_id() or ""),
                }
            # —— AI 自主收藏：有缺图需求才收，按概率、过合规、进核验区 ——
            if not self.config.get("fav_group_auto", True):
                return
            if not self._deficit_hint():
                return
            if umo in self._pending:
                return
            if self._quota_left() <= 0:
                return
            gap = float(self.config.get("fav_auto_cooldown_minutes", 30)) * 60
            last_auto = getattr(self, "_last_auto", None)
            if last_auto is None:
                last_auto = self._last_auto = {}
            if gap > 0 and time.time() - float(last_auto.get(umo, 0)) < gap:
                return
            text = (event.message_str or "").strip()
            # 合规闸门 + 看图评分都在 _collect_if_worthy 里；通过才入库/进核验区
            ok, note = await self._collect_if_worthy(
                imgs[0], umo, event=event, context_text=text,
                review=bool(self.config.get("fav_ai_collect_needs_review", True)),
                quota=True,
            )
            if not ok:
                logger.info(f"[sticker_pack] 群图自主收藏放弃：{note or '不合适'}")
                return
            self._last_auto[umo] = time.time()
            logger.info(f"[sticker_pack] 群图自主收藏「{note}」"
                        f"（{umo.rsplit(':', 1)[-1]}） 今日额度 {self._collect_quota.get('count')}/"
                        f"{self.config.get('fav_daily_limit', 100)}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 群图自动收藏失败: {type(e).__name__}: {e}")

    # ---------- LLM 工具：一个词换一张图 ----------
    @filter.llm_tool(name="send_meme")
    async def send_meme(self, event: AstrMessageEvent, mood: str = ""):
        """甩一张表情包（斗图/配图/表达情绪时用）。系统按你给的情绪自己挑最贴切的一张，
        你不需要挑、也不需要知道图的名字；调用完就当作已经发出去了，别在文字里再描述这张图。
        **想斗图、想让气氛活起来、觉得光回文字太干巴、被人逗乐想回敬一张时，都主动用它**，
        不用等人先发图，也不用等系统推荐。

        Args:
            mood(string): 想表达的情绪或场景，一个词最好：开心、得意、无语、吃瓜、震惊、生气、委屈、害羞、撒娇、摆烂、累了、想睡、饿了、干活、收到、阴阳。也可以写短句，例如「被夸了有点飘」「看到傻子言论」「不知道该说什么」。
        """
        mood = (mood or "").strip()
        if not mood:
            return "没说想表达什么情绪，随便给个词也行（开心/无语/累了）"
        umo = event.unified_msg_origin
        self._remember_bot(event, umo)
        # 微信没有 QQ 黄脸 / 超级表情段，只从图片里挑（整不了就不硬整）
        kinds = ("image",) if umo.split(":", 1)[0] != "aiocqhttp" else ("image", "super")
        # 检索 → 兜底池 → 直接取第一张，不走「候选列表让 AI 再选一次」
        # 先严格（0.85 整词），不行再放宽（0.55），最后随机兜底一张；
        # 私聊里被要求「发一张」时绝不能说「我发不了」
        names = self._match_candidates(
            mood, 3, umo, allow_fallback=False, min_score=0.85
        )
        if not names:
            # 严格没命中 → 放宽到 0.55 再试一次：私聊里人说的情绪词更花（「不服气甩屁股」这种），
            # 卡 0.85 整词等于「永远发不出来」，放宽比不发更像真人
            names = self._match_candidates(
                mood, 3, umo, allow_fallback=True, min_score=0.55
            )
        if not names:
            # 还是排不出来 → 直接随机兜底一张图，真人被要求「发一张」时不会说「我发不了」
            pool = [
                n for n, v in self.stickers.items()
                if v.get("kind") in kinds
            ]
            if pool:
                names = [random.choice(pool)]
        if not names:
            self._note_deficit(mood, umo)
            return f"库里没有能表达「{mood}」的表情，这次先不发，用文字说就行"
        errs: list[str] = []
        for name in names:
            item = self.stickers.get(name)
            if not item or item.get("kind") not in kinds:
                continue
            try:
                seg = self._to_payload(item)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {type(e).__name__}")
                continue
            if await self._push(umo, seg):
                self._mark_used(umo, name)
                self._mark_emit(umo)
                logger.info(f"[sticker_pack] send_meme「{mood}」-> {name}（{umo.rsplit(':', 1)[-1]}）")
                return f"已发「{name}」"
            errs.append(f"{name}: send_fail")
        logger.warning(f"[sticker_pack] send_meme 全部失败 mood={mood!r} errs={errs}")
        return "表情没发出去（图都被拒了），这次就用文字"
