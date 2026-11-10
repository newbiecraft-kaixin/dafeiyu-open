"""人味水群（human_sim 插件：白天档 / 夜宵档）+ /禁言 状态管理。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from ._admins import DATA, _LOCK, _read_json, _restart_astrbot, _write_json
from ._groups import _qq_groups

HUMAN_SIM_DIR = DATA / "plugins" / "astrbot_plugin_human_sim"
HUMAN_SIM_CONFIG = HUMAN_SIM_DIR / "config.json"
STATE_PATH = DATA / "plugin_data" / "astrbot_plugin_human_sim" / "state.json"

# key -> (显示名, 最小值, 最大值, 类型)
_HUMAN_SIM_NUM: Dict[str, Tuple[str, float, float, Any]] = {
    "start_hour": ("白天开始（点）", 0, 23, int),
    "end_hour": ("白天结束（点）", 0, 23, int),
    "night_frequency_scale": ("夜间频率系数", 0, 1, float),
    "ai_decide_probability": ("主动决策抽样", 0, 1, float),
    "ai_decide_cooldown_seconds": ("主动决策冷却（秒）", 0, 86400, int),
    "decision_check_interval_seconds": ("决策检查间隔（秒）", 1, 3600, int),
    "ai_decide_per_minute": ("主动决策每分钟", 0, 60, int),
    "followup_seconds": ("接话窗口（秒）", 0, 3600, int),
    "followup_max_messages": ("窗口内最多条数", 0, 20, int),
    "followup_min_gap_seconds": ("跟进最小间隔（秒）", 0, 600, int),
    "interject_probability": ("随机插话概率", 0, 1, float),
    "interject_cooldown_minutes": ("插话冷却（分钟）", 0, 1440, int),
    "group_idle_minutes": ("冷场判定（分钟）", 0, 1440, int),
    "group_idle_probability": ("冷场开话头概率", 0, 1, float),
    "solo_face_probability": ("单发表情概率", 0, 1, float),
    "solo_sticker_probability": ("表情包概率", 0, 1, float),
    "repeat_probability": ("复读概率", 0, 1, float),
    "repeat_cooldown_seconds": ("复读冷却（秒）", 0, 86400, int),
    "global_per_minute": ("全局每分钟上限", 0, 60, int),
    "global_gap_min": ("全局最小间隔（秒）", 0, 600, int),
    "global_gap_max": ("全局最大间隔（秒）", 0, 600, int),
    "pm_active_probability": ("私聊主动关心概率", 0, 1, float),
    "pm_active_cooldown_hours": ("私聊关心冷却（小时）", 0, 168, int),
}
_HUMAN_SIM_BOOL = {
    "ai_decide": "AI 自主决策",
    "repeat_enabled": "复读开关",
    "plus1_enabled": "+1 机械跟队（关 = 全交给 AI 判断）",
}


def _human_sim_defaults() -> Dict[str, Any]:
    """插件默认值（读不到插件源码时的兜底），只取本卡片暴露的键。"""
    return {
        "start_hour": 9,
        "end_hour": 23,
        "night_frequency_scale": 0.25,
        "ai_decide": True,
        "ai_decide_probability": 0.3,
        "ai_decide_cooldown_seconds": 90,
        "decision_check_interval_seconds": 8,
        "ai_decide_per_minute": 2,
        "followup_seconds": 180,
        "followup_max_messages": 8,
        "followup_min_gap_seconds": 10,
        "interject_probability": 0.02,
        "interject_cooldown_minutes": 45,
        "group_idle_minutes": 120,
        "group_idle_probability": 0.15,
        "solo_face_probability": 0.06,
        "solo_sticker_probability": 0.05,
        "repeat_enabled": True,
        "repeat_probability": 0.25,
        "repeat_cooldown_seconds": 600,
        "plus1_enabled": False,
        "global_per_minute": 3,
        "global_gap_min": 20,
        "global_gap_max": 60,
        "pm_active_probability": 0.2,
        "pm_active_cooldown_hours": 8,
    }


def _in_daytime(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def human_sim() -> Dict[str, Any]:
    """读取 human_sim 的水群参数 + 当前落在白天档还是夜宵档。"""
    defaults = _human_sim_defaults()
    cfg = _read_json(HUMAN_SIM_CONFIG) if HUMAN_SIM_CONFIG.exists() else {}
    values: Dict[str, Any] = {}
    for key in _HUMAN_SIM_NUM:
        raw = cfg.get(key, defaults[key])
        try:
            values[key] = _HUMAN_SIM_NUM[key][3](raw)
        except (TypeError, ValueError):
            values[key] = defaults[key]
    for key in _HUMAN_SIM_BOOL:
        values[key] = bool(cfg.get(key, defaults[key]))
    start, end = int(values["start_hour"]), int(values["end_hour"])
    hour = time.localtime().tm_hour
    night_scale = float(values["night_frequency_scale"])
    return {
        "config": values,
        "period": {
            "start_hour": start,
            "end_hour": end,
            "now_hour": hour,
            "is_day": _in_daytime(hour, start, end),
            "night_scale": night_scale,
            "night_silent": night_scale <= 0,
        },
        "plugin_online": HUMAN_SIM_DIR.exists(),
        "restart_required": True,
    }


def save_human_sim(payload: Dict[str, Any]) -> Dict[str, Any]:
    """只合并卡片暴露的键；其它配置原样保留，保存后必须重启 AstrBot 才生效。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是对象")
    with _LOCK:
        cfg = _read_json(HUMAN_SIM_CONFIG)
        for key, (label, low, high, cast) in _HUMAN_SIM_NUM.items():
            if key not in payload:
                continue
            try:
                value = cast(payload[key])
            except (TypeError, ValueError) as error:
                raise ValueError(f"{label}必须是数字") from error
            if value < low or value > high:
                raise ValueError(f"{label}必须在 {low} ~ {high} 之间")
            cfg[key] = value
        for key, label in _HUMAN_SIM_BOOL.items():
            if key in payload:
                cfg[key] = bool(payload[key])
        start = int(cfg.get("start_hour", 9))
        end = int(cfg.get("end_hour", 23))
        if start == end:
            raise ValueError("白天开始和结束不能相同（会变成全天有效）")
        gap_min = int(cfg.get("global_gap_min", 15))
        gap_max = int(cfg.get("global_gap_max", 45))
        if gap_min > gap_max:
            raise ValueError("全局最小间隔不能大于最大间隔")
        HUMAN_SIM_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        _write_json(HUMAN_SIM_CONFIG, cfg)
    _restart_astrbot()
    result = human_sim()
    result["restarted"] = True
    return result


def handle_human_sim(_req: Any) -> Any:
    from infra import ok

    return ok(human_sim())


def handle_human_sim_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    body = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    try:
        return ok(save_human_sim(body))
    except ValueError as error:
        return fail(str(error), 400)


# ---------- 禁言管理（跟群里发 /禁言 分钟 是同一套状态） ----------

def _mute_prefix(state: Dict[str, Any]) -> str:
    """从已有 mute 键推断本机 umo 的平台前缀（如 大肥鱼QQ:GroupMessage:）。"""
    for key in state:
        if key.startswith("mute_until:"):
            parts = key[len("mute_until:"):].rsplit(":", 2)
            if len(parts) == 3 and parts[1] == "GroupMessage":
                return f"{parts[0]}:GroupMessage:"
    return "大肥鱼QQ:GroupMessage:"


def mutes() -> Dict[str, Any]:
    """当前生效中的禁言列表 + QQ 群清单（给面板下拉用）。"""
    state = _read_json(STATE_PATH) if STATE_PATH.exists() else {}
    if not isinstance(state, dict):
        state = {}
    now = time.time()
    rows: List[Dict[str, Any]] = []
    for key, value in state.items():
        if not key.startswith("mute_until:"):
            continue
        try:
            until = float(value or 0)
        except (TypeError, ValueError):
            continue
        remaining = until - now
        if remaining <= 0:
            continue  # 过期键由插件落盘时自己清
        umo = key[len("mute_until:"):]
        parts = umo.rsplit(":", 2)
        kind = parts[1] if len(parts) == 3 else "GroupMessage"
        target = parts[2] if len(parts) == 3 else umo
        rows.append({
            "umo": umo,
            "kind": kind,
            "target_id": target,
            "target_name": target,
            "until": until,
            "remaining_minutes": round(remaining / 60.0, 1),
        })
    _qq_ok, qq_rows, _err = _qq_groups()
    name_map = {g["id"]: g["name"] for g in qq_rows}
    for row in rows:
        if row["kind"] == "GroupMessage" and row["target_id"] in name_map:
            row["target_name"] = name_map[row["target_id"]]
    rows.sort(key=lambda r: r["until"])
    return {
        "mutes": rows,
        "groups": [{"id": g["id"], "name": g["name"]} for g in qq_rows],
    }


def set_mute(group_id: Any, minutes: Any) -> Dict[str, Any]:
    """设置/解除禁言，直接写 human_sim 的 state.json；插件按 mtime 热同步。

    minutes <= 0 表示解除；group_id 传群号，或直接传 umo（解除任意会话）。
    """
    target = str(group_id or "").strip()
    if not target:
        raise ValueError("group_id 不能为空")
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise ValueError("时长必须是数字（分钟）")
    if minutes < 0 or minutes > 1440:
        raise ValueError("时长需在 0 ~ 1440 分钟之间（0 = 解除）")
    with _LOCK:
        state = _read_json(STATE_PATH) if STATE_PATH.exists() else {}
        if not isinstance(state, dict):
            state = {}
        if ":" in target:  # 完整 umo，原样管理
            umo = target
            parts = target.rsplit(":", 2)
            gid = parts[-1]
            kind = parts[1] if len(parts) == 3 else "GroupMessage"
        elif target.isdigit():
            gid = target
            kind = "GroupMessage"
            umo = f"{_mute_prefix(state)}{gid}"
        else:
            raise ValueError("group_id 要么是群号，要么是完整 umo")
        # 清掉同一会话的 mute 键（兼容历史平台名），再按需写入
        for key in list(state):
            if not key.startswith("mute_until:"):
                continue
            parts = key[len("mute_until:"):].rsplit(":", 2)
            if len(parts) == 3 and parts[1] == kind and parts[2] == gid:
                state.pop(key, None)
        if minutes > 0:
            state[f"mute_until:{umo}"] = time.time() + minutes * 60
        _write_json(STATE_PATH, state)
    return mutes()


def handle_mutes(_req: Any) -> Any:
    from infra import ok

    return ok(mutes())


def handle_mute_save(req: Any) -> Any:
    from infra import fail, ok

    payload = req.payload if isinstance(req.payload, dict) else {}
    try:
        return ok(set_mute(payload.get("group_id"), payload.get("minutes", 0)))
    except ValueError as error:
        return fail(str(error), 400)
