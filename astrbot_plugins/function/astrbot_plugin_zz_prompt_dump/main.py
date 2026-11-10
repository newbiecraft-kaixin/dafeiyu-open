import json
import os
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(PLUGIN_DIR, "config.json")

DEFAULTS = {
    "enabled": True,
    # 文本总 dump（追加式，看全局流）
    "output_path": os.path.join(os.path.expanduser("~"), "Desktop", "LLM请求dump.txt"),
    "max_bytes": 300000,
    # 按群独立落盘（llm-monitor 可视化程序读这里）
    "group_dump_enabled": True,
    "group_dump_dir": os.path.join(PLUGIN_DIR, "dump"),
    "group_keep": 1,
    "context_tail": 3,
    "dump_response": True,
}


def _parse_umo(umo: str) -> tuple[str, str]:
    """大肥鱼QQ:GroupMessage:1000000002 -> ("群", "1000000002")；私聊同理。"""
    parts = [p for p in str(umo or "").split(":") if p]
    if len(parts) >= 3:
        kind = "群" if "Group" in parts[-2] else "私聊"
        return kind, parts[-1]
    if len(parts) == 2:
        return "私聊", parts[-1]
    return "未知", (parts[-1] if parts else "unknown")


class _Cfg:
    data = dict(DEFAULTS)
    mtime = 0.0

    @classmethod
    def get(cls) -> dict:
        try:
            mt = os.path.getmtime(CFG_PATH)
            if mt != cls.mtime:
                with open(CFG_PATH, encoding="utf-8-sig") as f:
                    cls.data = {**DEFAULTS, **json.load(f)}
                cls.mtime = mt
        except Exception:
            cls.data = dict(DEFAULTS)
        return cls.data


def _txt_append(cfg: dict, lines: list[str]) -> None:
    text = "\n".join(lines) + "\n\n"
    out = cfg.get("output_path") or DEFAULTS["output_path"]
    out = os.path.expandvars(os.path.expanduser(out))
    try:
        if os.path.exists(out) and os.path.getsize(out) > int(cfg.get("max_bytes", 300000)):
            os.remove(out)
    except OSError:
        pass
    with open(out, "a", encoding="utf-8") as f:
        f.write(text)


def _group_append(cfg: dict, umo: str, entry: dict) -> None:
    """按群独立 JSON：dump/<kind>_<gid>.json，items 新的在前，保留 group_keep 条。"""
    if not cfg.get("group_dump_enabled", True):
        return
    kind, gid = _parse_umo(umo)
    dump_dir = cfg.get("group_dump_dir") or DEFAULTS["group_dump_dir"]
    os.makedirs(dump_dir, exist_ok=True)
    path = os.path.join(dump_dir, f"{kind}_{gid}.json")
    data: dict = {"items": []}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
            data = loaded
    except (OSError, ValueError):
        pass
    data["kind"], data["gid"] = kind, gid
    data["updated"] = entry["ts"]
    items: list = data["items"]
    items.insert(0, entry)
    # 按「轮」保留：group_keep 轮。items 新的在前，每轮 = [RESP, REQ] 相邻。
    # 找到第 keep+1 个 REQ 的位置连同更旧的一起裁掉；再删掉没有 REQ 配对的孤儿 RESP。
    keep = int(cfg.get("group_keep", 1) or 1)
    seen = 0
    cut = None
    last_req = -1
    for i, it in enumerate(items):
        if it.get("dir") == "req":
            last_req = i
            seen += 1
            if seen > keep:
                cut = i
                break
    if cut is not None:
        del items[cut:]
    else:
        del items[last_req + 1:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


@register(
    "prompt_dump",
    "kaixin",
    "调试（llm-monitor 配套）：LLM 请求/回复全链路 dump——文本总流 + 按群独立 JSON。zz_ 前缀保证最后加载，抓到全部插件注入完之后的最终内容",
    "1.1.0",
)
class PromptDump(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_llm_request()
    async def dump_request(self, event: AstrMessageEvent, req):
        try:
            cfg = _Cfg.get()
            if not cfg.get("enabled", True):
                return
            ts = time.strftime("%m-%d %H:%M:%S")
            umo = event.unified_msg_origin
            sp = getattr(req, "system_prompt", "") or ""
            pr = getattr(req, "prompt", "") or ""
            parts = [str(getattr(p, "text", "") or p)
                     for p in (getattr(req, "extra_user_content_parts", None) or [])]
            ctx = getattr(req, "contexts", None) or []
            tail = int(cfg.get("context_tail", 3) or 0)
            tail_json = []
            for m in ctx[-tail:] if tail > 0 else []:
                try:
                    tail_json.append(json.loads(json.dumps(m, ensure_ascii=False, default=str)))
                except Exception:
                    tail_json.append(str(m))
            entry = {
                "ts": ts,
                "dir": "req",
                "sender_id": str(event.get_sender_id() or ""),
                "sender_name": event.get_sender_name() or "",
                "system_prompt": sp,
                "prompt": pr,
                "extra": parts,
                "contexts_count": len(ctx),
                "contexts_tail": tail_json,
            }
            lines = [
                f"===== REQ {ts}  umo={umo} =====",
                f"sender={entry['sender_id']} name={entry['sender_name']}",
                f"[system_prompt {len(sp)} 字]", sp,
                f"[prompt {len(pr)} 字]", pr,
            ]
            for i, t in enumerate(parts):
                lines.append(f"[extra_user_content_parts[{i}] {len(t)} 字]")
                lines.append(t)
            lines.append(f"[contexts {len(ctx)} 条]")
            _txt_append(cfg, lines)
            _group_append(cfg, umo, entry)
        except Exception as e:  # noqa: BLE001
            print(f"[prompt_dump] req {type(e).__name__}: {e}")

    @filter.on_llm_response()
    async def dump_response(self, event: AstrMessageEvent, resp):
        try:
            cfg = _Cfg.get()
            if not cfg.get("enabled", True) or not cfg.get("dump_response", True):
                return
            ts = time.strftime("%m-%d %H:%M:%S")
            umo = event.unified_msg_origin
            # AstrBot 的 LLMResponse 没有 completion 字段，文本在 completion_text / result_chain 里
            text = str(getattr(resp, "completion_text", "") or "").strip()
            chain = getattr(resp, "result_chain", None)
            if not text and chain is not None:
                for fn in ("to_plain_text", "plain_text"):
                    getter = getattr(chain, fn, None)
                    if callable(getter):
                        text = str(getter() or "").strip()
                        if text:
                            break
                if not text:
                    text = str(chain)
            tools = [str(t) for t in (getattr(resp, "tools_call_name", None) or [])]
            entry = {"ts": ts, "dir": "resp", "reply": text, "tools": tools}
            lines = [
                f"===== RESP {ts}  umo={umo} =====",
                f"[tools {tools}]" if tools else "[tools 无]",
                f"[回复 {len(text)} 字]",
                text,
            ]
            _txt_append(cfg, lines)
            _group_append(cfg, umo, entry)
        except Exception as e:  # noqa: BLE001
            print(f"[prompt_dump] resp {type(e).__name__}: {e}")
