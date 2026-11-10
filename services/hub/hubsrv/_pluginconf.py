"""通用插件配置页：控制台在线读写各 AstrBot 插件目录下的 config.json。

为什么要有这页：表情存储、识图这类插件的参数此前只能手工改 config.json，
中枢控制台管不到（控制台原有的卡片只覆盖 human_sim / provider_router 等少数插件）。
这里扫描 DATA/plugins/astrbot_plugin_* 的 config.json 生成统一配置页；
保存时只允许改文件里已有的键，类型按现值收敛，避免把插件配置写坏。
插件是否热加载由各插件自己决定（内置 mtime 热加载的改完即生效，
没有的要在 AstrBot WebUI 重载插件）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from ._admins import DATA, _LOCK, _read_json, _write_json

PLUGINS_DIR = DATA / "plugins"
# 键名带这些词的值按敏感信息渲染（前端用密码框），不明文回显在屏幕上
_SECRET_RE = re.compile(r"key|token|secret|password|cookie", re.I)
_ID_RE = re.compile(r"[\w.-]+")

# 配置键中文名：插件 config.json 的键大多是英文，控制台展示时翻成中文，
# 没收录的键原样显示英文。键是全局的（不同插件同名键含义相同才共用一条）。
FIELD_LABELS: Dict[str, str] = {
    # —— 通用 ——
    "enabled": "启用",
    "owner_id": "主人 ID",
    "owner_ids": "管理员列表",
    "max_chars": "最大字符数",
    "group_ids": "生效群列表",
    "watch_groups": "监听群列表",
    "unlimited_groups": "不限流群列表",
    "prefer_provider_id": "指定对话模型",
    # —— human_sim 模拟真人 ——
    "ai_decide": "AI 自主决定发言",
    "ai_decide_cooldown_seconds": "AI 决策冷却（秒）",
    "ai_decide_per_minute": "AI 决策每分钟上限",
    "ai_decide_probability": "AI 决策概率",
    "at_last_speaker_probability": "回复时 @ 上一发言人概率",
    "at_random_member_probability": "随机 @ 群友概率",
    "at_reply_probability": "回复时 @ 对方概率",
    "audience_activity_max": "活跃群消息上限（条/分）",
    "audience_activity_min": "活跃群消息下限（条/分）",
    "audience_activity_window": "活跃度统计窗口（分钟）",
    "audience_max_size": "群规模上限（人）",
    "audience_min_factor": "群规模最小系数",
    "audience_min_size": "群规模下限（人）",
    "audience_scaling": "按群人数缩放频率",
    "avatar_local": "头像本地缓存",
    "balance_empty_notice_cooldown_minutes": "余额不足提醒冷却（分钟）",
    "balance_empty_notice_enabled": "余额不足提醒开关",
    "bot_names": "机器人昵称",
    "decision_check_interval_seconds": "决策检查间隔（秒）",
    "decision_layer": "决策层（规则/AI）",
    "directed_reply_delay_seconds": "定向回复延迟（秒）",
    "emoji_ai_pick": "AI 挑表情包",
    "emoji_ai_pick_candidates": "AI 挑表情候选数",
    "emoji_ai_pick_history": "表情历史条数",
    "emoji_ai_pick_timeout": "AI 挑表情超时（秒）",
    "end_hour": "活动结束时间（时）",
    "face_probability": "发 QQ 表情概率",
    "followup_max_messages": "追问最多条数",
    "followup_min_gap_seconds": "追问最小间隔（秒）",
    "followup_seconds": "追问等待时间（秒）",
    "global_gap_max": "全局发言间隔上限（秒）",
    "global_gap_min": "全局发言间隔下限（秒）",
    "global_per_minute": "全局每分钟发言上限",
    "greeting_group": "迎新群",
    "greeting_slots": "迎新话术槽",
    "group_idle_minutes": "群冷场判定（分钟）",
    "group_idle_per_minute": "冷场开话每分钟上限",
    "group_idle_probability": "冷场开话概率",
    "group_leave_notify": "退群提醒开关",
    "image_repeat_probability": "图片复读概率",
    "interest_echo_probability": "兴趣附和概率",
    "interest_learn": "兴趣学习开关",
    "interest_repeat_max_chars": "兴趣复读最大字符",
    "interest_repeat_min_weight": "兴趣复读最小权重",
    "interject_cooldown_minutes": "潜水插话冷却（分钟）",
    "interject_per_minute": "潜水插话每分钟上限",
    "interject_probability": "潜水插话概率",
    "like_at_target": "点赞时 @ 对方",
    "like_reply_ai": "点赞回复用 AI",
    "like_reply_max_chars": "点赞回复最大字符",
    "like_request_local": "点赞请求走本地模型",
    "listen_window_reply_group": "监听窗口回复群",
    "local_probe": "本地探测开关",
    "long_form_hints": "长回复触发词",
    "long_max_chars": "长回复最大字符",
    "lurk_check_interval_minutes": "潜水检查间隔（分钟）",
    "member_notice_enabled": "成员变动提醒开关",
    "mute_default_minutes": "默认禁言时长（分钟）",
    "night_frequency_scale": "夜间降频系数",
    "plus1_enabled": "+1 功能开关",
    "plus1_gap_seconds": "+1 最小间隔（秒）",
    "plus1_max_chars": "+1 最大字符",
    "plus1_window_seconds": "+1 时间窗口（秒）",
    "pm_active_cooldown_hours": "私聊主动冷却（小时）",
    "pm_active_enable": "私聊主动开话开关",
    "pm_active_idle_minutes": "私聊闲置判定（分钟）",
    "pm_active_max_age_hours": "私聊主动最大时限（小时）",
    "pm_active_probability": "私聊主动开话概率",
    "poke_active": "主动拍一拍开关",
    "poke_active_cooldown_minutes": "主动拍冷却（分钟）",
    "poke_active_probability": "主动拍概率",
    "poke_back_probability": "回拍概率",
    "poke_cooldown_seconds": "拍一拍冷却（秒）",
    "poke_directed_boost": "被点名时拍一拍加成",
    "poke_repeat_window_seconds": "连拍判定窗口（秒）",
    "poke_request_local": "拍一拍走本地模型",
    "poke_respond": "被拍必回开关",
    "poke_to_chat_probability": "戳戳接入正式对话概率",
    "poke_sticker_probability": "被拍回表情包概率",
    "poke_text_probability": "被拍回文字概率",
    "probe_cache_seconds": "探测缓存（秒）",
    "react_after_reply_probability": "回完话顺手拍概率",
    "react_only_probability": "只反应不回话概率",
    "react_random_decide": "随机决定反应方式",
    "recharge_link_base": "充值链接前缀",
    "recharge_mode": "充值模式",
    "recharge_owner_only": "仅主人可充值",
    "recharge_python": "充值脚本 Python 路径",
    "recharge_qr_path": "收款码图片路径",
    "recharge_qr_timeout": "收款码超时（秒）",
    "recharge_script": "充值脚本路径",
    "recharge_tiers": "充值档位",
    "recharge_url": "充值页面地址",
    "repeat_cooldown_seconds": "复读冷却（秒）",
    "repeat_enabled": "复读开关",
    "repeat_probability": "复读概率",
    "reply_delay_seconds": "回复延迟（秒）",
    "self_topic_boost": "自身话题加成",
    "self_topic_keywords": "自身话题关键词",
    "send_min_gap_seconds": "发送最小间隔（秒）",
    "sender_rate_cooldown_seconds": "同一人发言冷却（秒）",
    "sender_rate_max_directed": "同一人定向回复上限",
    "sender_rate_window_seconds": "定向回复统计窗口（秒）",
    "shared_minute_cap": "共享每分钟限额",
    "silence_ramp": "冷场渐静开关",
    "silence_ramp_full_minutes": "完全安静时长（分钟）",
    "silence_ramp_start_minutes": "渐静开始时间（分钟）",
    "silent_keepalive_probability": "静默保活概率",
    "solo_face_probability": "单独发 QQ 表情概率",
    "solo_sticker_probability": "单独发表情包概率",
    "start_hour": "活动开始时间（时）",
    "sticker_back_cooldown_seconds": "回表情包冷却（秒）",
    "sticker_back_probability": "回表情包概率",
    "sticker_probability": "发表情包概率",
    "sticker_request_local": "表情请求走本地模型",
    "super_face_request_local": "超级表情走本地模型",
    # —— group_window 群聊窗口 ——
    "inject_index": "注入起始位置",
    "inject_max_chars": "注入最大字符",
    "inject_max_messages": "注入最大条数",
    "include_bot_messages": "记录机器人消息",
    "max_messages": "窗口最大条数",
    "save_interval": "保存间隔（秒）",
    # —— speaker_tag 用户画像 ——
    "history_max_records": "历史最大记录数",
    "user_inject_chars": "用户画像注入字符",
    "user_keep_msgs": "用户保留消息数",
    "user_max_users": "用户画像人数上限",
    "user_memory": "用户记忆开关",
    "user_min_msgs": "建画像最少消息数",
    "user_recent_inject": "注入最近发言",
    "user_save_interval": "画像保存间隔（秒）",
    # —— qzone_channel 空间动态 ——
    "history_turns": "历史轮数",
    "max_feeds": "最多拉取动态数",
    "max_replies_per_feed": "每条动态最多回复",
    "max_replies_per_round": "每轮最多回复",
    "min_comment_chars": "评论最少字符",
    "owner_notify": "通知主人开关",
    "poll_interval_minutes": "轮询间隔（分钟）",
    "reply_enabled": "自动回复开关",
    "system_prompt": "回复提示词",
    "view_auto_like": "浏览时自动点赞",
    "view_default_count": "默认浏览条数",
    "view_max_count": "浏览条数上限",
    # —— daily_qzone 定时空间 ——
    "catchup_minutes": "补发窗口（分钟）",
    "check_interval_minutes": "检查间隔（分钟）",
    "daily_summary": "每日总结开关",
    "jitter_minutes": "随机抖动（分钟）",
    "latest_hour": "最晚发布时间（时）",
    "llm_retry": "模型重试次数",
    "post_hours": "发布时间点（时）",
    "post_prompt": "空间人格提示词",
    "summary_prompt": "空间总结提示词",
    "persona_id": "人格",
    "themes": "可选主题",
    "ugc_right": "UGC 权限开关",
    # —— web_lookup 联网 ——
    "bili_browse_default": "B站默认浏览页数",
    "bili_browse_max": "B站浏览页数上限",
    "bili_cookie": "B站 Cookie",
    "bili_dwell_max": "B站单页停留上限（秒）",
    "bili_dwell_ratio": "B站停留比例",
    "bili_dwell_total_max": "B站总停留上限（秒）",
    "bili_pool_max": "B站链接池上限",
    "bili_subtitle_chars": "B站字幕最大字符",
    "bili_tags": "B站关注标签",
    "concurrency": "并发数",
    "keep_n": "保留条数",
    "max_results": "最大结果数",
    "page_cap_kb": "页面大小上限（KB）",
    "search_timeout_s": "搜索超时（秒）",
    "timeout_s": "请求超时（秒）",
    "trust_env": "信任系统代理",
    "video_audio_max_mb": "视频音频上限（MB）",
    "video_audio_max_s": "视频音频上限（秒）",
    "video_comment_n": "视频评论条数",
    "video_parse_timeout_s": "视频解析超时（秒）",
    "video_read_max_s": "视频阅读上限（秒）",
    "video_read_min_s": "视频阅读下限（秒）",
    "video_read_ratio": "视频阅读比例",
    "video_transcript_chars": "视频字幕最大字符",
    # —— sticker_pack 表情包 ——
    "auto_face_min_chars": "自动表情最少字符",
    "auto_face_probability": "自动表情概率",
    "deficit_ttl_minutes": "欠账过期（分钟）",
    "explore_probability": "探索新表情概率",
    "face_min_gap_seconds": "表情最小间隔（秒）",
    "face_text_per_face": "QQ 表情配文模板",
    "face_whitelist_only": "仅白名单表情",
    "fav_ai_collect_needs_review": "AI 收藏需审核",
    "fav_auto_cooldown_minutes": "自动收藏冷却（分钟）",
    "fav_auto_probability": "自动收藏概率",
    "fav_daily_limit": "每日收藏上限",
    "fav_enabled": "表情收藏开关",
    "fav_group_auto": "群内自动收藏",
    "fav_max_ratio": "收藏相似上限",
    "fav_min_side": "收藏最小边长（px）",
    "fav_novelty_boost": "新颖度加成",
    "fav_pending_timeout_minutes": "待审超时（分钟）",
    "fav_quality_gate": "收藏质量闸",
    "fav_similarity_max": "收藏相似度上限",
    "fav_vision_check": "收藏识图检查",
    "follow_up_candidates": "跟发候选数",
    "follow_up_cooldown_seconds": "跟发冷却（秒）",
    "follow_up_per_minute": "跟发每分钟上限",
    "follow_up_probability": "跟发表情概率",
    "hash_rescan_minutes": "哈希重扫间隔（分钟）",
    "image_sub_type": "图片子类型",
    "image_summary": "图片摘要开关",
    "max_stickers_per_message": "单条消息表情上限",
    "narrow_pool_threshold": "窄池阈值",
    "recent_window": "最近使用窗口",
    "review_ttl_days": "审核保留天数",
    "short_reply_char_limit": "短回复字数上限",
    "short_reply_solo_probability": "短回复单独表情概率",
    "sticker_density_window_seconds": "表情密度窗口（秒）",
    "sticker_min_gap_seconds": "表情最小间隔（秒）",
    "sticker_text_per_sticker": "表情包配文模板",
    "text_match_core_score": "文字匹配核心分",
    "text_match_min_score": "文字匹配最低分",
    "text_match_topk": "文字匹配候选数",
    "vision_model": "识图模型",
    # —— wechat_oc_bridge 微信桥 ——
    "allow_chats": "允许的会话",
    "enable_group": "群聊开关",
    "io_base": "桥接服务地址",
    # —— tool_gate 工具闸 ——
    "open_provider_ids": "开放模型的 ID",
    "open_providers": "开放模型开关",
    "owner_full_access": "主人全权限",
    "provider_bind_skip_prefixes": "跳过模型绑定前缀",
    # —— zz_prompt_dump 提示词转储 ——
    "context_tail": "上下文尾部条数",
    "dump_response": "记录模型回复",
    "group_dump_dir": "群转储目录",
    "group_dump_enabled": "群转储开关",
    "group_keep": "群保留条数",
    "max_bytes": "单文件上限（字节）",
    "output_path": "输出路径",
    # —— basic_tools / face_convert ——
    "weather_default_city": "默认天气城市",
    "video_enabled": "视频功能开关",
    # —— provider_router 模型路由 ——
    "block_groups": "黑名单群",
    "block_users": "黑名单用户",
    "deepseek_group_ids": "走强模型的群",
    "deepseek_provider_id": "云端强模型",
    "group_persona_ids": "按群锁定人格",
    "group_provider_chain": "普通群模型链",
    "group_provider_id": "群聊模型",
    "group_provider_ids": "按群固定模型",
    "listen_groups": "仅监听这些群",
    "classify_timeout": "分类超时（秒）",
    "health_cooldown": "故障冷却（秒）",
    "health_probe_enabled": "健康探测开关",
    "health_probe_interval": "探测间隔（秒）",
    "health_probe_timeout": "探测超时（秒）",
    "private_provider_id": "私聊模型",
    "short_len_threshold": "短消息阈值（字）",
    "simple_provider_id": "简单消息模型",
    "smart_route_enabled": "大小模型切换开关",
    "stranger_private_provider_id": "陌生人私聊模型",
    "strong_len_threshold": "长消息阈值（字）",
    "use_classifier": "用小模型分类",
    "user_persona_ids": "按好友锁定人格",
    "user_provider_ids": "按人固定模型",
    "wechat_persona_id": "微信人格",
    "wechat_provider_id": "微信模型",
}


def _meta(folder: Path) -> Dict[str, str]:
    """从 metadata.yaml 抠 name/desc/version；没有就退回目录名。"""
    meta = {"name": folder.name, "desc": "", "version": ""}
    path = folder / "metadata.yaml"
    if not path.exists():
        return meta
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return meta
    for key in ("name", "desc", "version"):
        m = re.search(rf"^{key}:\s*[\"']?(.*?)[\"']?\s*$", text, re.M)
        if m and m.group(1).strip():
            meta[key] = m.group(1).strip()
    return meta


def _field_type(val: Any) -> str:
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "float"
    if isinstance(val, list):
        return "list"
    if isinstance(val, dict):
        return "json"
    return "str"


def _coerce(old: Any, new: Any, key: str) -> Any:
    """按旧值类型收敛新值；转不动就报错，绝不让类型漂移写坏插件配置。"""
    if isinstance(old, bool):
        if isinstance(new, bool):
            return new
        low = str(new).strip().lower()
        if low in ("true", "1", "on", "开", "是"):
            return True
        if low in ("false", "0", "off", "关", "否", ""):
            return False
        raise ValueError(f"{key} 应为 true/false")
    if isinstance(old, int):
        try:
            return int(float(str(new).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 应为整数") from None
    if isinstance(old, float):
        try:
            return float(str(new).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} 应为数字") from None
    if isinstance(old, list):
        if isinstance(new, list):
            return new
        if isinstance(new, str):
            return [p for p in re.split(r"[,，、\n]+", new.strip()) if p]
        raise ValueError(f"{key} 应为列表")
    if isinstance(old, str):
        return "" if new is None else str(new)
    # dict 等复杂值整块原样提交（前端用 JSON 文本框编辑）
    return new


def _alt_config_path(pid: str) -> Path:
    """AstrBot 约定的另一类配置路径：data/config/astrbot_plugin_*_config.json。

    livingmemory / web_lookup / dafeiyu_pet / humanizer 用这种，
    插件目录下没有 config.json 的就只能在这里改。
    """
    return DATA / "config" / f"{pid}_config.json"


def _config_path(pid: str) -> Path:
    """插件配置的真实路径：插件目录 config.json 优先，否则 data/config 下找。"""
    plugin_cfg = PLUGINS_DIR / pid / "config.json"
    if plugin_cfg.exists():
        return plugin_cfg
    return _alt_config_path(pid)


def plugins_configs() -> Dict[str, Any]:
    """所有有配置的插件：元信息 + 字段清单（类型由现值推断）。

    覆盖两种配置形态：插件目录下的 config.json，以及 AstrBot 约定的
    data/config/astrbot_plugin_*_config.json（同一插件两种都存在时取插件目录）。
    """
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    if not PLUGINS_DIR.exists():
        return {"plugins": rows}
    for folder in sorted(
        p for p in PLUGINS_DIR.iterdir()
        if p.is_dir() and p.name.startswith("astrbot_plugin_")
    ):
        cfg_path = folder / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = _read_json(cfg_path)
        except ValueError:
            continue
        fields = [
            {
                "key": key,
                "label": FIELD_LABELS.get(key, key),
                "type": _field_type(cfg[key]),
                "value": cfg[key],
                "secret": bool(_SECRET_RE.search(key)),
            }
            for key in cfg
        ]
        rows.append({"id": folder.name, **_meta(folder), "fields": fields})
        seen.add(folder.name)
    alt_dir = DATA / "config"
    if alt_dir.exists():
        for path in sorted(alt_dir.glob("astrbot_plugin_*_config.json")):
            pid = path.name[: -len("_config.json")]
            if pid in seen:
                continue
            try:
                cfg = _read_json(path)
            except ValueError:
                continue
            if not cfg:
                continue
            fields = [
                {
                    "key": key,
                    "label": FIELD_LABELS.get(key, key),
                    "type": _field_type(cfg[key]),
                    "value": cfg[key],
                    "secret": bool(_SECRET_RE.search(key)),
                }
                for key in cfg
            ]
            folder = PLUGINS_DIR / pid
            meta = _meta(folder) if folder.is_dir() else {"name": pid, "desc": "", "version": ""}
            rows.append({"id": pid, **meta, "fields": fields})
            seen.add(pid)
    return {"plugins": rows}


def save_plugin_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """保存单个插件的 config.json：只合并已有键，类型按现值收敛。"""
    pid = str((payload or {}).get("id") or "").strip()
    if not pid or not _ID_RE.fullmatch(pid) or ".." in pid:
        raise ValueError("非法插件名")
    incoming = (payload or {}).get("config")
    if not isinstance(incoming, dict):
        raise ValueError("config 必须是对象")
    cfg_path = _config_path(pid)
    if not cfg_path.exists():
        raise ValueError("插件不存在或没有 config.json")
    with _LOCK:
        cfg = _read_json(cfg_path)
        for key, val in incoming.items():
            if key not in cfg:
                raise ValueError(f"未知配置项 {key}（只允许修改已有键）")
            cfg[key] = _coerce(cfg[key], val, key)
        _write_json(cfg_path, cfg)
    return {"id": pid, "saved": len(incoming)}


def handle_plugins(_req: Any) -> Any:
    from infra import ok

    return ok(plugins_configs())


def handle_qzone_scan(_req: Any) -> Any:
    """中枢里打开「空间动态」配置时触发：给插件留一个请求文件，插件看到就拉登录二维码。"""
    from infra import fail, ok

    flag = DATA / "plugin_data" / "qzone_login" / "scan_request"
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1", encoding="utf-8")
    except OSError as error:
        return fail(f"写入登录请求失败: {error}", 500)
    return ok({"requested": True, "qr": str(flag.parent / "qr.png")})


def handle_plugins_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(save_plugin_config(payload))
    except ValueError as error:
        return fail(str(error), 400)
