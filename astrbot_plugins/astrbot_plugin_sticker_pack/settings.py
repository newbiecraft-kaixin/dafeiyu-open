"""插件路径、出站节流与默认配置常量。"""

from __future__ import annotations

import json
import os
import re

from astrbot.api import logger

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))


def _load_send_gate():
    """跨插件出站节流（与 human_sim / humanizer 共用同一实例）。"""
    import importlib.util as _ilu
    import sys as _sys

    name = "fy_send_gate"
    if name in _sys.modules:
        try:
            return _sys.modules[name].get_gate()
        except Exception:  # noqa: BLE001
            return None
    path = os.path.join(DATA_ROOT, "plugin_data", "fy_send_gate.py")
    try:
        spec = _ilu.spec_from_file_location(name, path)
        mod = _ilu.module_from_spec(spec)
        _sys.modules[name] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.get_gate()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[sticker_pack] 发送节流模块加载失败（不节流）: {e}")
        return None


send_gate = _load_send_gate()
PLACEHOLDER = re.compile(r":([^:\s]{1,12}):")
# 模型时不时把冒号打成全角（：西瓜：），半角正则认不出，整串就以字面量发进群里。
# 只认「库里确实有的名字」才归一，避免把「注意：这个问题：很严重」这种正常标点也吃掉。
FW_PLACEHOLDER = re.compile(r"：([^：:\s]{1,12})：")

# 「别笑了」「不要阴阳我」这类否定句：字面命中了「笑」，情绪却是反的，
# 照字面配一张狂笑图就成了抬杠。命中就干脆不配，让文字自己说话。
NEG_MOOD_RE = re.compile(
    r"(别|不要|不准|甭|少|不许|没)(再)?(笑|闹|夸|阴阳|嘲讽|装|卖萌|贫|嘴)"
)

# data/plugins/astrbot_plugin_sticker_pack -> data/stickers（Windows 原生推导，不再写死容器路径）
DEFAULT_CONFIG = {
    "pack_dir": os.path.normpath(os.path.join(PLUGIN_DIR, "..", "..", "stickers")),
    "map_file": "map.txt",
    "inject_index": False,   # 不再把表情索引塞给 AI：表情由系统在发完话后分析推荐，AI 只做选择
    # —— 发完话后的表情推荐流程（系统分析文本 → 推荐候选 → AI 挑 → 单独发一条）——
    "follow_up_probability": 0.5,       # 触发概率（每条文字回复；群友发表情挺勤的）
    "follow_up_per_minute": 3,          # 每分钟补表情上限（防刷屏/防递归兜底）
    "follow_up_cooldown_seconds": 45,   # 同群两次补表情最小间隔（无限制群也生效，间隔×2）
    "follow_up_delay_seconds": [2, 5],  # 发完文字后隔多久走推荐流程（像真人打完字再补表情）
    "follow_up_candidates": 5,          # 推荐给 AI 的候选数量（固定 5 张，每条带描述）
    # —— 超级表情（QQ 大表情）并入同一条推荐途径（补表情时和普通表情包同池竞争）——
    "super_faces_enabled": True,
    "super_face_boost": 1.25,           # 语义命中时的分数加成，让大表情别总被表情包挤掉
    # —— 推荐层：纯文字相似度（AI 写的字 vs 全库「名字+含义」，绝不碰图片指纹）——
    "text_match_core_score": 0.85,      # ≥此分算「整词命中」，第一批全部进池
    "text_match_min_score": 0.15,       # 情绪字扫尾层的最低分，低于此值宁可空手（顺带记「缺图需求」）
    "text_match_topk": 24,              # 单个情绪词最多留多少张进池（池越大越活泼，太大会跑题）
    "narrow_pool_threshold": 4,         # 池子小于这个数时，小概率从全库捞冷门图
    "fav_novelty_boost": 1.12,          # 收藏库新图的分数加成（收进来就能被抽到）
    # —— 表情单独成条（真人不会把贴图塞在句子里）——
    "max_stickers_per_message": 1,      # 一条回复最多配几个表情（硬上限，超出直接丢）
    "sticker_min_gap_seconds": 30,      # 同群两次表情之间的最小间隔（单发/配脸/补表情共用）
    "image_sub_type": 1,                # 图片段 sub_type：1=当表情发（单条纯表情=超大表情），0=普通图片
    "image_summary": "[动画表情]",       # 表情在 QQ 里的摘要
    "solo_face_probability": 0.5,       # 黄脸单独成条的概率（QQ 会把单条纯表情渲染成超大表情）
    # —— 语义配脸：不要求模型写 :词: ，系统自己从正文切词判断情绪 ——
    "auto_face_probability": 0.35,      # 正文里没占位符时，自动补一张黄脸的概率
    "auto_face_min_chars": 4,           # 正文短于这个字数不配脸（「嗯」「好」配张脸很怪）
    "face_whitelist_only": True,        # 只允许常用脸（斜眼笑/瓢虫这类冷门直接换掉或丢弃）
    "solo_delay_seconds": [0.8, 2.6],   # 文字发出后隔多久单甩表情
    "short_reply_char_limit": 6,        # 去掉占位符后正文不超过这么多字，就算「短回复」
    "short_reply_solo_probability": 0.85,  # 短回复时把表情拎出来单发（纯表情=超大表情）
    # —— 推荐多样性（治「一直用那几个」）——
    "recent_window": 30,                # 同群记住最近 N 个发过的名字，轮换冷却
    "explore_probability": 0.1,         # 候选太少 / 完全没对上时，小概率从全库捞一张冷门图
    # —— 收藏（算法自主：图片到达 → 质量/合规/查重 → 看图评分决定收不收 → 入 fav/）——
    "fav_enabled": True,
    "fav_similarity_max": 0.90,         # dHash 相似度高于此值视为重复，直接拒收
    "fav_group_auto": True,             # 群里按缺图需求自主收图（不看关键词）
    "fav_auto_probability": 0.5,        # 兼容保留；自主收藏改由看图评分决定，不再靠随机数
    "fav_daily_limit": 100,             # 自主收藏每日上限（群图自动收；私聊/主人发的图不占额度）
    "fav_manual_window_minutes": 30,    # 兼容保留（旧手动收藏窗口，当前流程不再使用）
    "fav_pending_timeout_minutes": 10,  # 下载后等待入库的最长时间，超时作废
    "review_ttl_days": 7,               # 待核验区（fav/_review）的图超过这么多天没核验就自动删除
    "hash_rescan_minutes": 30,          # 指纹与清理的巡检周期（分钟）：库外部改动也能跟上
    "deficit_ttl_minutes": 30,          # 「缺图需求」的有效期
    # AI 自主判定的群图先进 fav/_review 待核验；关掉则判定通过就直接入库
    "fav_ai_collect_needs_review": True,
    # —— 收藏质量闸门（宁缺毋滥：糊图/长截图/广告/低俗图一律不进库）——
    "fav_quality_gate": True,
    "fav_min_side": 100,                # 短边小于这个像素 = 缩略图/小图标
    "fav_max_ratio": 3.5,               # 长宽比超过这个 = 长截图/长条图
    "fav_min_bytes": 4096,              # 小于 4KB 多半是压缩过度的糊图
    "fav_max_bytes": 10485760,          # 大于 10MB 不收
    "fav_vision_check": True,           # 有视觉模型时先粗筛一遍（失败自动放行）
    "fav_ban_words": (
        "色情,涩情,黄图,裸照,露点,约炮,嫖娼,强奸,迷奸,偷拍,血腥,砍死,"
        "自杀,赌博,博彩,毒品,冰毒,代开发票,贷款,加微信,刷单,返利"
    ),
}
