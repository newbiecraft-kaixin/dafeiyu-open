# -*- coding: utf-8 -*-
"""《大肥鱼表情库》本地服务：存储（表情 CRUD）为主，附带只读辅助接口。

接入管理已并入中枢（http://127.0.0.1:23408/ui），旧 /access 入口只做跳转，
不再提供写 access.json 的表单，避免旧页面用旧结构覆盖中枢新增的 levels 等字段。

功能：
  GET  /  /storage        -> 存储.html（表情增删改查）
  GET  /access            -> 302 跳转到中枢
  GET  /api/stickers      -> 表情索引（map.txt + fav.txt），含月份
  GET  /api/providers     -> provider 列表（只读）
  GET  /api/personas      -> 人设列表（只读）
  GET  /api/access        -> 群接入配置（只读）
  GET  /api/fallback      -> 备用链（只读）
  GET  /<相对路径>         -> data/stickers/ 下静态文件（图片预览）
  POST /api/sticker/*     -> 表情增 / 删 / 改 + 上传
  POST /api/provider/test -> provider 连通性测试
  POST /api/fallback      -> 备用链保存

用法：python tools_source_server.py   （默认端口 8712）
"""
import base64, datetime, io, json, os, re, shutil, sqlite3, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8712
JSON_PATH = os.path.join(HERE, "_src_data.json")
MAP_PATH = os.path.join(HERE, "map.txt")
STORAGE_HTML = os.path.join(HERE, "存储.html")
HUB_UI = "http://127.0.0.1:23408/ui"
DATA_DIR = os.path.dirname(HERE)                                   # .../data
ACCESS_PATH = os.path.join(DATA_DIR, "local_tools", "access.json")
TOOLNAMES_PATH = os.path.join(DATA_DIR, "local_tools", "tool_names.json")
CMD_CONFIG = os.path.join(DATA_DIR, "cmd_config.json")
FAV_MAP_PATH = os.path.join(HERE, "fav", "map_fav.txt")
AI_DRAFTS_PATH = os.path.join(HERE, "ai_drafts.json")
AI_CFG_PATH = os.path.join(HERE, "ai_config.json")     # 页面里填的云端视觉接口（base/key/model）
AI_MODEL = os.environ.get("DFY_VL_MODEL", "qwen3.5:9b")
AI_PROMPT = (
    "你是表情包整理助手。看这张图，用中文给出两样东西：\n"
    "1) name：2~8 个字的触发词，描述画面里的动作或表情（例：干饭、抱大腿、无语）\n"
    "2) desc：不超过 30 字，说明什么场景适合发这张图\n"
    '只输出 JSON：{"name":"...","desc":"..."}，不要解释、不要 markdown。'
)
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434").rstrip("/")
IMG_EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")
# 与插件 astrbot_plugin_sticker_pack/main.py 的 _fingerprint 保持同一算法：
# md5 + dHash + 中心 dHash + aHash + 颜色直方图（动图只取第一帧）。
# 上传入口先拿它跟插件写好的全库指纹缓存比一遍，拦住「换名字再传一遍」的重复图。
FP_CACHE = os.path.join(HERE, "fav", "_hash_v3.json")
FP_THR = 0.90


def _fp_image(data: bytes) -> dict:
    import hashlib
    n = 16
    md5 = hashlib.md5(data).hexdigest()
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            rgb = im.convert("RGB")
            gray = rgb.convert("L")
            db = gray.resize((n + 1, n), Image.Resampling.LANCZOS).tobytes()
            w, h = rgb.size
            cw, ch = max(1, int(w * 0.92)), max(1, int(h * 0.92))
            x0, y0 = (w - cw) // 2, (h - ch) // 2
            d2b = gray.crop((x0, y0, x0 + cw, y0 + ch)).resize(
                (n + 1, n), Image.Resampling.LANCZOS).tobytes()
            ab = gray.resize((n, n), Image.Resampling.LANCZOS).tobytes()
            cb = rgb.resize((32, 32), Image.Resampling.BILINEAR).tobytes()
    except Exception:
        return {"m": md5}
    bits = d2 = abits = 0
    stride = n + 1
    for r in range(n):
        base = r * stride
        for c in range(n):
            bits = (bits << 1) | (1 if db[base + c] > db[base + c + 1] else 0)
            d2 = (d2 << 1) | (1 if d2b[base + c] > d2b[base + c + 1] else 0)
    mean = sum(ab) / max(1, len(ab))
    for v in ab:
        abits = (abits << 1) | (1 if v > mean else 0)
    hist = [0] * 64
    for i in range(0, len(cb) - 2, 3):
        hist[(cb[i] >> 6) * 16 + (cb[i + 1] >> 6) * 4 + (cb[i + 2] >> 6)] += 1
    return {"m": md5, "d": f"{bits:064x}", "d2": f"{d2:064x}",
            "a": f"{abits:064x}", "c": hist}


def _bits_sim(a, b):
    if not a or not b:
        return -1.0
    try:
        return 1.0 - bin(int(a, 16) ^ int(b, 16)).count("1") / 256.0
    except ValueError:
        return -1.0


def _hist_sim(a, b):
    if not a or not b:
        return -1.0
    try:
        x = [float(v) for v in a]
        y = [float(v) for v in b]
    except (TypeError, ValueError):
        return -1.0
    dot = sum(i * j for i, j in zip(x, y))
    nx = sum(i * i for i in x) ** 0.5
    ny = sum(j * j for j in y) ** 0.5
    if nx <= 0 or ny <= 0:
        return -1.0
    return max(0.0, min(1.0, dot / (nx * ny)))


def _fp_sim(a, b) -> float:
    if not a or not b:
        return 0.0
    if isinstance(a, str):
        a = {"d": a}
    if isinstance(b, str):
        b = {"d": b}
    if a.get("m") and b.get("m") and a["m"] == b["m"]:
        return 1.0
    acc = wsum = 0.0
    for key, w in (("d", 0.45), ("a", 0.25), ("c", 0.30)):
        if key == "d":
            s = _bits_sim(a.get("d"), b.get("d"))
            s2 = _bits_sim(a.get("d2"), b.get("d2"))
            if s2 > s:
                s = s2
        elif key == "a":
            s = _bits_sim(a.get("a"), b.get("a"))
        else:
            s = _hist_sim(a.get("c"), b.get("c"))
        if s < 0:
            continue
        acc += s * w
        wsum += w
    return acc / wsum if wsum else 0.0


def _fp_dup(fp: dict):
    """跟插件落盘的指纹缓存比一遍；命中返回 (名字, 相似度)。"""
    try:
        with io.open(FP_CACHE, encoding="utf-8") as f:
            lib = json.load(f)
    except Exception:
        return "", 0.0
    best, best_key = 0.0, ""
    for key, other in (lib or {}).items():
        s = _fp_sim(fp, other)
        if s > best:
            best, best_key = s, key
    if best <= FP_THR:
        return "", best
    label = best_key
    if label.startswith("@review:"):
        base = os.path.splitext(label.split(":", 1)[1])[0]
        label = "待核验/" + (base.rsplit("_", 1)[0] or "未命名")
    return label, best


V4DB_PATH = os.path.join(DATA_DIR, "data_v4.db")
PROTECTED_PERSONAS = {"default"}      # 禁止删除的人设
STICKERS_DIR = HERE
FALLBACK_TOOLS = [
    "local_list_dir", "local_read_file", "local_write_file",
    "local_append_note", "local_sys_info", "local_screenshot", "local_calc",
]
ALLOW_FIELDS = {"name", "desc", "month", "kind", "src"}
EXT_TYPES = {
    ".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp",
    ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, fp):
        ext = os.path.splitext(fp)[1].lower()
        with io.open(fp, "rb") as f:
            body = f.read()
        self._send(200, body, EXT_TYPES.get(ext, "application/octet-stream"))

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        from urllib.parse import unquote
        p = unquote(self.path.split("?")[0])
        if p in ("/", "/index.html", "/storage", "/storage.html"):
            return self._file(STORAGE_HTML)
        if p in ("/access", "/access.html", "/接入管理.html"):
            # 接入管理已并入中枢：旧书签直接跳转，不再返回旧表单
            self.send_response(302)
            self.send_header("Location", HUB_UI)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p in ("/health", "/healthz"):
            # 中枢按 /health 判在线，同时把表情张数带回去显示在服务卡片上
            return self._json(200, {"ok": True, "service": "stickers",
                                    "port": PORT, "stats": self._stats()})
        if p == "/api/stats":
            return self._json(200, {"ok": True, **self._stats()})
        if p == "/api/data":
            with io.open(JSON_PATH, "rb") as f:
                return self._send(200, f.read())
        if p == "/api/access":
            if not os.path.isfile(ACCESS_PATH):
                return self._json(200, {
                    "groups": {}, "default_level": "standard",
                    "roles": {"group": "standard", "friend": "standard", "stranger": "guest"},
                })
            with io.open(ACCESS_PATH, "rb") as f:
                return self._send(200, f.read())
        if p == "/api/tools":
            if os.path.isfile(TOOLNAMES_PATH):
                with io.open(TOOLNAMES_PATH, "rb") as f:
                    return self._send(200, f.read())
            return self._json(200, {"exported_at": 0, "tools": FALLBACK_TOOLS})
        if p == "/api/providers":
            return self._json(200, self._providers())
        if p == "/api/stickers":
            return self._json(200, self._stickers())
        if p == "/api/sticker/ai_scan":
            return self._ai_scan_status()
        if p == "/api/sticker/ai_drafts":
            return self._json(200, {"ok": True, "drafts": self._drafts_load()})
        if p == "/api/sticker/ai_config":
            return self._json(200, self._ai_status())
        if p == "/api/personas":
            return self._json(200, self._personas())
        if p == "/api/fallback":
            return self._json(200, self._fallback_get())
        fp = os.path.normpath(os.path.join(HERE, p.lstrip("/").replace("/", os.sep)))
        if not fp.startswith(HERE) or not os.path.isfile(fp):
            return self._json(404, {"err": "not found: " + p})
        return self._file(fp)

    @staticmethod
    def _mask_key(v):
        s = str(v or "")
        if len(s) <= 8:
            return "已配置" if s else ""
        return s[:5] + "..." + s[-3:] + f" (len{len(s)})"

    @classmethod
    def _providers(cls):
        """AstrBot v4 provider 列表 + 密钥来源掩码（只读，改 key 去 WebUI）。"""
        try:
            with io.open(CMD_CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            return {"err": f"read cmd_config: {e}", "providers": [], "sources": []}
        sources = {}
        for s in cfg.get("provider_sources", []) or []:
            k = s.get("key")
            k0 = k[0] if isinstance(k, list) and k else (k if isinstance(k, str) else "")
            sources[s.get("id", "")] = {
                "id": s.get("id", ""), "provider": s.get("provider", ""),
                "type": s.get("type", ""), "api_base": s.get("api_base", ""),
                "enable": s.get("enable", True), "key": cls._mask_key(k0),
            }
        provs = []
        for p in cfg.get("provider", []) or []:
            pt = (p.get("provider_type") or "").lower()
            pid = p.get("id", "")
            if "tts" in pid.lower() or pt == "text_to_speech":
                continue  # 接入管理只关心聊天模型
            provs.append({
                "id": pid, "model": p.get("model", ""),
                "enable": p.get("enable", True),
                "source": p.get("provider_source_id", ""),
                "modalities": p.get("modalities") or [],
            })
        default_pid = ""
        ar = (cfg.get("agent_runner") or {}).get("config", {}).get("model", {})
        default_pid = ar.get("provider_id", "")
        return {"providers": provs, "sources": sources, "default_provider_id": default_pid}

    # ---------- 人设管理（data_v4.db personas 表）----------
    @staticmethod
    def _db():
        return sqlite3.connect(V4DB_PATH, timeout=10)

    def _personas(self):
        try:
            db = self._db()
            rows = db.execute(
                "SELECT persona_id, system_prompt, length(system_prompt), "
                "begin_dialogs, tools FROM personas ORDER BY id").fetchall()
            db.close()
        except Exception as e:
            return {"err": f"read db: {e}", "personas": []}
        out = []
        for pid, prompt, plen, begin, tools in rows:
            out.append({
                "persona_id": pid, "prompt": prompt or "", "prompt_len": plen or 0,
                "begin_dialogs": json.loads(begin) if begin else [],
                "tools": json.loads(tools) if tools else None,   # 工具白名单(null=不限制)
            })
        # 全局默认人设 + 备用链一起带上
        glob = {"global_persona_id": "", "fallback": []}
        try:
            with io.open(CMD_CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            ar = cfg.get("agent_runner", {}).get("config", {})
            glob["global_persona_id"] = ar.get("persona", {}).get("persona_id", "")
            glob["fallback"] = ar.get("model", {}).get("fallback_provider_ids", []) or []
        except Exception:
            pass
        return {"personas": out, **glob}

    def _persona_op(self, op):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"err": "bad request body"})
        pid = str(req.get("persona_id") or "").strip()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        if op == "save":
            prompt = str(req.get("prompt") or "").strip()
            if not pid or not prompt:
                return self._json(400, {"err": "persona_id/prompt 必填"})
            if len(prompt) > 20000:
                return self._json(400, {"err": "人设最长 20000 字"})
            db = self._db()
            cur = db.execute(
                "UPDATE personas SET system_prompt=?, updated_at=? WHERE persona_id=?",
                (prompt, now, pid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                return self._json(404, {"err": "人设不存在: " + pid})
            return self._json(200, {"ok": True, "note": "对已有会话的 system_prompt 会在下轮请求生效"})

        if op == "add":
            prompt = str(req.get("prompt") or "").strip()
            if not pid or not prompt:
                return self._json(400, {"err": "persona_id/prompt 必填"})
            if not (2 <= len(pid) <= 30) or not all(c.isalnum() or c in "-_" for c in pid):
                return self._json(400, {"err": "人设 ID 限 2~30 位字母数字-_"})
            db = self._db()
            if db.execute("SELECT 1 FROM personas WHERE persona_id=?", (pid,)).fetchone():
                db.close()
                return self._json(400, {"err": "人设 ID 已存在"})
            db.execute(
                "INSERT INTO personas (created_at, updated_at, persona_id, system_prompt, "
                "begin_dialogs, tools, skills, sort_order) VALUES (?,?,?,?,?,?,?,0)",
                (now, now, pid, prompt, "[]", None, None))
            db.commit(); db.close()
            return self._json(200, {"ok": True})

        if op == "delete":
            if pid in PROTECTED_PERSONAS:
                return self._json(400, {"err": "系统人设不可删除"})
            # 被接入配置引用的也拒删
            try:
                with io.open(ACCESS_PATH, encoding="utf-8") as f:
                    acc = json.load(f)
                used_by = [g for g, c in (acc.get("groups") or {}).items()
                           if (c or {}).get("persona_id") == pid]
                if used_by:
                    return self._json(400, {"err": f"被群 {','.join(used_by)} 引用，先解绑再删"})
            except Exception:
                pass
            db = self._db()
            cur = db.execute("DELETE FROM personas WHERE persona_id=?", (pid,))
            db.commit(); db.close()
            if cur.rowcount == 0:
                return self._json(404, {"err": "人设不存在: " + pid})
            return self._json(200, {"ok": True})

        return self._json(400, {"err": "unknown op"})

    # ---------- 全局备用链（cmd_config agent_runner）----------
    @staticmethod
    def _fallback_get():
        try:
            with io.open(CMD_CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            return {"fallback": cfg["agent_runner"]["config"]["model"].get(
                "fallback_provider_ids", []) or []}
        except Exception as e:
            return {"err": str(e), "fallback": []}

    def _fallback_save(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"err": "bad request body"})
        chain = req.get("fallback_provider_ids")
        if not isinstance(chain, list) or any(not isinstance(x, str) for x in chain):
            return self._json(400, {"err": "fallback_provider_ids 必须是字符串列表"})
        chain = [x.strip() for x in chain if x.strip()][:6]
        with io.open(CMD_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        valid = {p.get("id") for p in cfg.get("provider", [])}
        bad = [x for x in chain if x not in valid]
        if bad:
            return self._json(400, {"err": "不存在的 provider: " + ",".join(bad)})
        shutil.copyfile(CMD_CONFIG, CMD_CONFIG + ".bak_edit")
        cfg["agent_runner"]["config"]["model"]["fallback_provider_ids"] = chain
        with io.open(CMD_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return self._json(200, {"ok": True, "fallback": chain,
                                "note": "需重启 AstrBot 后生效"})

    # ---------- provider 手动管理（写 cmd_config.json，重启生效）----------
    @staticmethod
    def _cfg_load():
        with io.open(CMD_CONFIG, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _cfg_save(cfg):
        shutil.copyfile(CMD_CONFIG, CMD_CONFIG + ".bak_edit")
        with io.open(CMD_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _provider_op(self, kind, op):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"err": "bad request body"})
        try:
            cfg = self._cfg_load()
        except Exception as e:
            return self._json(500, {"err": f"read cmd_config: {e}"})
        NOTE = "已写入 cmd_config.json，重启 AstrBot 后生效"

        if kind == "source":
            sources = cfg.setdefault("provider_sources", [])
            sid = str(req.get("id") or "").strip()
            if op == "add":
                if not sid or not all(c.isalnum() or c in "-_" for c in sid):
                    return self._json(400, {"err": "源 ID 必填，限字母数字-_"})
                if any(s.get("id") == sid for s in sources):
                    return self._json(400, {"err": "源 ID 已存在"})
                stype = str(req.get("type") or "openai_chat_completion").strip()
                api_base = str(req.get("api_base") or "").strip()
                key = str(req.get("key") or "").strip()
                if not api_base:
                    return self._json(400, {"err": "API 地址必填"})
                entry = {
                    "provider": sid.replace("-", "_"), "type": stype,
                    "provider_type": "embedding" if "embedding" in stype else "chat_completion",
                    "key": [key] if key else "ollama" if "ollama" in sid else "",
                    "api_base": api_base, "timeout": 120, "proxy": "",
                    "custom_headers": {}, "id": sid, "enable": True,
                }
                if "embedding" in stype:
                    entry["embedding_api_key"] = key or "ollama"
                    entry["embedding_api_base"] = api_base
                sources.append(entry)
                self._cfg_save(cfg)
                return self._json(200, {"ok": True, "note": NOTE})
            if op == "save":
                hit = next((s for s in sources if s.get("id") == sid), None)
                if not hit:
                    return self._json(404, {"err": "源不存在: " + sid})
                api_base = str(req.get("api_base") or "").strip()
                if api_base:
                    hit["api_base"] = api_base
                    if "embedding_api_base" in hit:
                        hit["embedding_api_base"] = api_base
                key = str(req.get("key") or "").strip()
                if key:  # 留空 = 不改密钥
                    hit["key"] = [key] if isinstance(hit.get("key"), list) else key
                    if "embedding_api_key" in hit:
                        hit["embedding_api_key"] = key
                if "enable" in req:
                    hit["enable"] = bool(req["enable"])
                self._cfg_save(cfg)
                return self._json(200, {"ok": True, "note": NOTE})
            if op == "delete":
                used = [p.get("id") for p in cfg.get("provider", [])
                        if p.get("provider_source_id") == sid]
                if used:
                    return self._json(400, {"err": "先删掉引用它的模型实例: " + ", ".join(map(str, used))})
                before = len(sources)
                cfg["provider_sources"] = [s for s in sources if s.get("id") != sid]
                if len(cfg["provider_sources"]) == before:
                    return self._json(404, {"err": "源不存在: " + sid})
                self._cfg_save(cfg)
                return self._json(200, {"ok": True, "note": NOTE})
            return self._json(400, {"err": "unknown op"})

        # ---- 模型实例 ----
        provs = cfg.setdefault("provider", [])
        pid = str(req.get("id") or "").strip()
        if op == "add":
            src = str(req.get("source_id") or "").strip()
            model = str(req.get("model") or "").strip()
            if not src or not model:
                return self._json(400, {"err": "所属源 / 模型名必填"})
            srcs = cfg.get("provider_sources", [])
            sinfo = next((s for s in srcs if s.get("id") == src), None)
            if not sinfo:
                return self._json(400, {"err": "所属源不存在: " + src})
            inst_id = str(req.get("inst_id") or "").strip() or f"{src}/{model}"
            if any(p.get("id") == inst_id for p in provs):
                return self._json(400, {"err": "实例 ID 已存在: " + inst_id})
            mods = [m for m in (req.get("modalities") or [])
                    if m in ("text", "image", "tool_use")] or ["text"]
            entry = {
                "id": inst_id, "enable": True, "provider_source_id": src,
                "model": model, "modalities": mods,
                "max_context_tokens": int(req.get("max_context_tokens") or 0),
                "model_config": {},
            }
            if sinfo.get("type") == "openai_chat_completion":
                entry["custom_extra_body"] = {}
            if "ollama" in src:
                entry["ollama_disable_thinking"] = True
            provs.append(entry)
            self._cfg_save(cfg)
            return self._json(200, {"ok": True, "id": inst_id, "note": NOTE})
        if op == "save":
            hit = next((p for p in provs if p.get("id") == pid), None)
            if not hit:
                return self._json(404, {"err": "实例不存在: " + pid})
            model = str(req.get("model") or "").strip()
            if model:
                hit["model"] = model
            if isinstance(req.get("modalities"), list):
                mods = [m for m in req["modalities"] if m in ("text", "image", "tool_use")]
                if mods:
                    hit["modalities"] = mods
            if "enable" in req:
                hit["enable"] = bool(req["enable"])
            if "max_context_tokens" in req:
                try:
                    hit["max_context_tokens"] = max(0, int(req["max_context_tokens"] or 0))
                except (TypeError, ValueError):
                    pass
            self._cfg_save(cfg)
            return self._json(200, {"ok": True, "note": NOTE})
        if op == "default":
            hit = next((p for p in provs if p.get("id") == pid), None)
            if not hit:
                return self._json(404, {"err": "实例不存在: " + pid})
            ar = cfg.setdefault("agent_runner", {}).setdefault("config", {})
            ar.setdefault("model", {})["provider_id"] = pid
            self._cfg_save(cfg)
            return self._json(200, {"ok": True, "note": NOTE + "（默认模型已切到 " + pid + "）"})
        if op == "delete":
            ar = (cfg.get("agent_runner") or {}).get("config", {})
            if ar.get("model", {}).get("provider_id") == pid:
                return self._json(400, {"err": "它是当前默认 provider，先换默认再删"})
            fb = ar.get("model", {}).get("fallback_provider_ids") or []
            if pid in fb:
                return self._json(400, {"err": "它在备用链里，先从备用链移除再删"})
            before = len(provs)
            cfg["provider"] = [p for p in provs if p.get("id") != pid]
            if len(cfg["provider"]) == before:
                return self._json(404, {"err": "实例不存在: " + pid})
            note = ""
            try:
                with io.open(ACCESS_PATH, encoding="utf-8") as f:
                    acc = json.load(f)
                used = [g for g, c in (acc.get("groups") or {}).items()
                        if (c or {}).get("provider_id") == pid]
                if used:
                    note = "；注意群 " + ",".join(used) + " 还绑着它，记得改绑定"
            except Exception:
                pass
            self._cfg_save(cfg)
            return self._json(200, {"ok": True, "note": NOTE + note})
        return self._json(400, {"err": "unknown op"})

    # ---------- 表情包解释 CRUD ----------
    _stats_cache = {"at": 0.0, "value": None}

    @classmethod
    def _stats(cls):
        """表情张数 / 分组数，给中枢卡片用：和存储页「共 N 张」同一个数。

        hub 每 5 秒来问一次，全库扫描只算一次，30 秒内复用。
        """
        now = time.time()
        if cls._stats_cache["value"] and now - cls._stats_cache["at"] < 30:
            return dict(cls._stats_cache["value"])
        try:
            data = cls._stickers()
            value = {"stickers": int(data.get("total") or 0),
                     "indexed": int(data.get("indexed") or 0),
                     "loose": int(data.get("loose") or 0),
                     "months": len(data.get("months") or [])}
        except Exception as e:  # noqa: BLE001
            value = {"error": "统计失败: %s" % e}
        cls._stats_cache.update(at=now, value=value)
        return dict(value)

    @staticmethod
    def _parse_map(path, scope):
        """解析索引文件 → 记录列表。行格式：名称:意思:目标
        目标 = face:N（黄脸）或相对图片路径。"""
        recs = []
        try:
            lines = io.open(path, encoding="utf-8").read().splitlines()
        except FileNotFoundError:
            return recs
        for i, ln in enumerate(lines):
            if ln.startswith("#") or ln.strip() == "" or ":" not in ln:
                continue
            parts = ln.split(":", 2)
            if len(parts) != 3:
                continue
            name, desc, target = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if target.startswith("face:") and target[5:].isdigit():
                recs.append({"line_no": i, "kind": "face", "name": name, "desc": desc,
                             "target": target, "face_id": target[5:], "scope": scope})
            elif any(target.lower().endswith(e) for e in IMG_EXTS):
                fp = os.path.normpath(os.path.join(HERE, target.replace("/", os.sep)))
                recs.append({"line_no": i, "kind": "file", "name": name, "desc": desc,
                             "target": target, "scope": scope, "exists": os.path.isfile(fp)})
            else:
                recs.append({"line_no": i, "kind": "unknown", "name": name, "desc": desc,
                             "target": target, "scope": scope})
        return recs

    @classmethod
    def _stickers(cls):
        # 官方黄脸（face:编号）不在这里管：它是 QQ 原生动态表情，名称/ID 由插件
        # 和 face_catalog.json 维护，本页只列需要人工命名/增删的图片条目。
        recs = [r for r in cls._parse_map(MAP_PATH, "map") + cls._parse_map(FAV_MAP_PATH, "fav")
                if r.get("kind") != "face"]
        # 合并 _src_data.json 的月份信息（仅图片类有）
        months = set()
        src_months = {}
        try:
            with io.open(JSON_PATH, encoding="utf-8") as f:
                for r in json.load(f).get("imgs", []):
                    rel = (r.get("dir", "") + "/" + r.get("file", "")).replace("\\", "/")
                    if r.get("month"):
                        src_months[rel] = r["month"]
                        months.add(r["month"])
        except Exception:
            pass
        names = {}
        for r in recs:
            names[r["name"]] = names.get(r["name"], 0) + 1
        for r in recs:
            r["dup"] = names[r["name"]] > 1
            if r.get("kind") == "file":
                rel = r["target"].replace("\\", "/")
                if rel in src_months:
                    r["month"] = src_months[rel]
                    months.add(src_months[rel])
                fp = os.path.normpath(os.path.join(HERE, rel.replace("/", os.sep)))
                if os.path.isfile(fp):
                    try:
                        r["mtime"] = datetime.datetime.fromtimestamp(
                            os.path.getctime(fp)).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        r["mtime"] = ""
        # 扫描磁盘上没写进 map 的图片（私藏/未入库），一并展示
        referenced = {r["target"].replace("\\", "/").strip().lower()
                      for r in recs if r.get("kind") == "file"}
        skip_dirs = {"__pycache__", "_trash", "_deprecated", "_gif_preview", "fav_old",
                     "_static_archive"}   # 归档区是淘汰帧，不参与发送，别混进待核验/一键识别
        drafts = cls._drafts_load()
        loose = []
        for dirpath, dirnames, filenames in os.walk(HERE):
            rel_dir = os.path.relpath(dirpath, HERE)
            parts = [] if rel_dir == "." else rel_dir.split(os.sep)
            if parts and (parts[0] in skip_dirs or any(p.startswith(".") for p in parts)):
                dirnames[:] = []
                continue
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in IMG_EXTS:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), HERE).replace(os.sep, "/")
                if rel.lower() in referenced:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    mt = datetime.datetime.fromtimestamp(
                        os.path.getctime(fp)).strftime("%Y-%m-%d %H:%M")
                    kb = int(os.path.getsize(fp) / 1024)
                except Exception:
                    mt, kb = "", 0
                rec = {
                    "line_no": None, "kind": "file", "name": os.path.splitext(fn)[0],
                    "desc": "", "target": rel, "scope": "disk", "exists": True,
                    "dup": False, "mtime": mt, "kb": kb,
                }
                d = drafts.get(rel)
                if d:
                    rec["draft"] = d
                loose.append(rec)
        loose.sort(key=lambda r: (r["mtime"] or ""), reverse=True)
        recs += loose
        return {"records": recs, "total": len(recs), "months": sorted(months),
                "indexed": len(recs) - len(loose), "loose": len(loose)}

    @staticmethod
    def _map_path_of(scope):
        return FAV_MAP_PATH if scope == "fav" else MAP_PATH

    @classmethod
    def _sync_src_data(cls, rel, name=None, desc=None, delete=False):
        """图片行改动时同步 _src_data.json（按 dir/file 匹配）。返回是否命中。"""
        try:
            with io.open(JSON_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        rel_n = rel.replace("\\", "/")
        hit = False
        keep = []
        for r in data.get("imgs", []):
            r_rel = (r.get("dir", "") + "/" + r.get("file", "")).replace("\\", "/")
            if r_rel == rel_n:
                hit = True
                if delete:
                    continue
                if name is not None:
                    r["name"] = name
                if desc is not None:
                    r["desc"] = desc
            keep.append(r)
        if not hit:
            return False
        if delete:
            data["imgs"] = keep
        shutil.copyfile(JSON_PATH, JSON_PATH + ".bak_edit")
        with io.open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True

    def _sticker_op(self, op):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"err": "bad request body"})
        # 未入库图片（scope=disk）：不在任何索引里，删除 = 丢文件 + 清 AI 草稿
        if op == "delete" and str(req.get("scope") or "") == "disk":
            target = str(req.get("target") or "").strip().replace("\\", "/")
            if not target:
                return self._json(400, {"err": "target 必填"})
            removed = self._delete_file(target)
            try:
                drafts = self._drafts_load()
                if drafts.pop(target, None) is not None:
                    self._drafts_save(drafts)
            except Exception:
                pass
            return self._json(200, {"ok": True, "note": "已丢弃未入库图片：" + removed})
        scope = req.get("scope") if req.get("scope") in ("map", "fav") else "map"
        path = self._map_path_of(scope)
        target = str(req.get("target") or "").strip()

        if op == "add":
            name = str(req.get("name") or "").strip()
            desc = str(req.get("desc") or "").strip()
            kind = req.get("kind")
            if not name or not desc or not target:
                return self._json(400, {"err": "name/desc/target 必填"})
            if len(name) > 40 or len(desc) > 120:
                return self._json(400, {"err": "名称≤40字 / 含义≤120字"})
            if ":" in name:
                return self._json(400, {"err": "名称不能含冒号（会破坏 :名称: 索引格式）"})
            if kind == "face":
                if not (target.startswith("face:") and target[5:].isdigit()):
                    return self._json(400, {"err": "黄脸目标必须是 face:数字，如 face:1"})
            elif kind == "file":
                if not target.lower().endswith(IMG_EXTS):
                    return self._json(400, {"err": "目标必须是图片（" + " / ".join(IMG_EXTS) + "）"})
                fp = os.path.normpath(os.path.join(HERE, target.replace("/", os.sep)))
                if not fp.startswith(HERE) or not os.path.isfile(fp):
                    return self._json(400, {"err": "图片文件不存在（相对 stickers/ 目录，如 pool/x.gif）"})
            else:
                return self._json(400, {"err": "kind 必须是 face 或 file"})
            # 允许同名：:名称: 会命中多条，发送时随机挑一张同名的
            shutil.copyfile(path, path + ".bak_edit")
            with io.open(path, "a", encoding="utf-8") as f:
                f.write("%s:%s:%s\n" % (name, desc, target))
            if kind == "file":
                tgt = target.replace("\\", "/")
                d, fn = tgt.split("/", 1) if "/" in tgt else ("", tgt)
                try:
                    with io.open(JSON_PATH, encoding="utf-8") as f:
                        data = json.load(f)
                    if not any(r.get("dir") == d and r.get("file") == fn for r in data.get("imgs", [])):
                        data.setdefault("imgs", []).append(
                            {"name": name, "desc": desc, "dir": d, "file": fn,
                             "ext": os.path.splitext(fn)[1], "month": "", "kind": "", "src": "manual"})
                        with io.open(JSON_PATH, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=1)
                except Exception:
                    pass
                # 入库后这张的 AI 草稿没用了，清掉避免旧草稿继续显示
                try:
                    drafts = self._drafts_load()
                    if drafts.pop(target.replace("\\", "/"), None) is not None:
                        self._drafts_save(drafts)
                except Exception:
                    pass
            return self._json(200, {"ok": True, "note": "机器人重载插件后生效"})

        if not target:
            return self._json(400, {"err": "target 必填"})
        # 精确身份 = 名称 + 目标（同一路径可能挂多条不同名称记录，只准动指定那条）
        orig_name = str(req.get("orig_name") or "").strip()
        lines = io.open(path, encoding="utf-8").read().splitlines()
        out, hits = [], 0
        for ln in lines:
            parts = ln.split(":", 2)
            is_match = (len(parts) == 3 and not ln.startswith("#")
                        and parts[2].strip() == target
                        and (not orig_name or parts[0].strip() == orig_name))
            if is_match and hits == 0:
                hits += 1
                if op == "delete":
                    continue
                name = str(req.get("name") or parts[0]).strip()[:40]
                desc = str(req.get("desc") or parts[1]).strip()[:120]
                out.append("%s:%s:%s" % (name, desc, parts[2]))
                continue
            out.append(ln)
        if not hits:
            return self._json(404, {"err": "未找到该条目（可能已被删除）"})
        if op != "delete":
            new_name = str(req.get("name") or "").strip()
            if ":" in new_name:
                return self._json(400, {"err": "名称不能含冒号（会破坏 :名称: 索引格式）"})
        shutil.copyfile(path, path + ".bak_edit")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        synced_src = False
        if any(target.lower().endswith(e) for e in IMG_EXTS):
            if op == "delete":
                synced_src = self._sync_src_data(target, delete=True)
                if req.get("delete_file"):
                    removed = self._delete_file(target)
                    if isinstance(removed, str):
                        return self._json(200, {"ok": True, "lines": hits,
                                                "srcSynced": synced_src,
                                                "note": "已删索引并彻底删除文件：" + removed})
            else:
                synced_src = self._sync_src_data(
                    target, name=req.get("name"), desc=req.get("desc"))
        return self._json(200, {"ok": True, "lines": hits, "srcSynced": synced_src,
                                "note": "机器人重载插件后生效"})

    @staticmethod
    def _delete_file(rel):
        """从磁盘彻底删除 stickers/ 下的图片文件（不可恢复）。返回文件名或错误说明。"""
        fp = os.path.normpath(os.path.join(HERE, rel.replace("/", os.sep)))
        if not fp.startswith(HERE) or not os.path.isfile(fp):
            return "文件不存在，跳过"
        # 还有别的索引行引用同一文件时不删文件
        for mp in (MAP_PATH, FAV_MAP_PATH):
            try:
                for ln in io.open(mp, encoding="utf-8").read().splitlines():
                    parts = ln.split(":", 2)
                    if len(parts) == 3 and not ln.startswith("#") and parts[2].strip() == rel:
                        return "其他条目仍在引用该文件，只删了索引"
            except FileNotFoundError:
                pass
        os.remove(fp)
        return os.path.basename(fp)

    def _sticker_upload(self):
        """上传本机图片 → 存入 pool/upload/（或 fav/）→ 写索引 + 来源数据。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"err": "bad request body"})
        scope = req.get("scope") if req.get("scope") in ("map", "fav") else "map"
        name = str(req.get("name") or "").strip()
        desc = str(req.get("desc") or "").strip()
        fname = os.path.basename(str(req.get("filename") or "")).strip()
        b64 = str(req.get("data_b64") or "").split(",")[-1].strip()
        if not (name and desc and fname and b64):
            return self._json(400, {"err": "名称/含义/文件/图片数据必填"})
        if len(name) > 40 or len(desc) > 120:
            return self._json(400, {"err": "名称≤40字 / 含义≤120字"})
        if ":" in name:
            return self._json(400, {"err": "名称不能含冒号（会破坏 :名称: 索引格式）"})
        ext = os.path.splitext(fname)[1].lower()
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in IMG_EXTS:
            return self._json(400, {"err": "只支持 " + " / ".join(IMG_EXTS)})
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return self._json(400, {"err": "图片数据解码失败"})
        if len(raw) > 10 * 1024 * 1024:
            return self._json(400, {"err": "图片超过 10MB"})
        dup, sim = _fp_dup(_fp_image(raw))
        if dup:
            return self._json(400, {"err": "库里已有几乎一样的图：%s（相似 %d%%），别重复入库"
                                    % (dup, round(sim * 100))})
        sub = "fav" if scope == "fav" else os.path.join("pool", "upload")
        os.makedirs(os.path.join(HERE, sub), exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|\s]+', "_", os.path.splitext(fname)[0])[:60] or "img"
        dest = os.path.join(HERE, sub, stem + ext)
        i = 1
        while os.path.exists(dest):
            dest = os.path.join(HERE, sub, f"{stem}_{i}{ext}")
            i += 1
        target = os.path.relpath(dest, HERE).replace("\\", "/")
        path = self._map_path_of(scope)
        with open(dest, "wb") as f:
            f.write(raw)
        shutil.copyfile(path, path + ".bak_edit")
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}:{desc}:{target}\n")
        d, fn = target.split("/", 1)
        try:
            with io.open(JSON_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("imgs", []).append(
                {"name": name, "desc": desc, "dir": d, "file": fn, "ext": ext,
                 "month": "", "kind": "", "src": "upload"})
            shutil.copyfile(JSON_PATH, JSON_PATH + ".bak_edit")
            with io.open(JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return self._json(200, {"ok": True, "target": target, "size": len(raw),
                                "note": "机器人重载插件后生效"})

    # ---------- 一键 AI 识别（本地视觉模型出草稿，人工核验后才入库） ----------
    _ai_lock = threading.Lock()
    _ai_model = AI_MODEL          # 探测到的本地可视觉模型，没有就用默认名
    _ai_state = {"running": False, "total": 0, "done": 0, "failed": 0, "skipped": 0,
                 "current": "", "started": 0, "finished": 0, "model": AI_MODEL,
                 "stop": False, "errors": []}

    @classmethod
    def _drafts_load(cls):
        try:
            with io.open(AI_DRAFTS_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    @classmethod
    def _drafts_save(cls, drafts):
        tmp = AI_DRAFTS_PATH + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(drafts, f, ensure_ascii=False, indent=1)
        os.replace(tmp, AI_DRAFTS_PATH)

    @staticmethod
    def _img_b64(rel, max_side=768):
        """把任意格式（gif/webp/…）转成 JPEG 再给视觉模型，避免格式不支持。"""
        from PIL import Image
        fp = os.path.normpath(os.path.join(HERE, rel.replace("/", os.sep)))
        if not fp.startswith(HERE) or not os.path.isfile(fp):
            return None, "文件不存在"
        try:
            im = Image.open(fp)
            try:
                im.seek(0)          # 动图只取第一帧
            except Exception:
                pass
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode("ascii"), None
        except Exception as e:
            return None, "图片读取失败: %s" % str(e)[:80]

    @classmethod
    def _ai_cfg_load(cls):
        try:
            with io.open(AI_CFG_PATH, encoding="utf-8") as f:
                c = json.load(f)
            if isinstance(c, dict):
                return {"base": str(c.get("base") or "").strip().rstrip("/"),
                        "key": str(c.get("key") or "").strip(),
                        "model": str(c.get("model") or "").strip()}
        except Exception:
            pass
        return {"base": "", "key": "", "model": ""}

    @classmethod
    def _ai_cfg_save(cls, cfg):
        tmp = AI_CFG_PATH + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, AI_CFG_PATH)

    @classmethod
    def _ai_cloud(cls):
        """云端 OpenAI 兼容视觉接口三项配齐了就用它，否则回落到本机 ollama。"""
        c = cls._ai_cfg_load()
        return c if (c["base"] and c["key"] and c["model"]) else None

    @classmethod
    def _ai_status(cls):
        """给页面判断「要不要先弹配置窗」：ready=False 时 reason 说明缺什么。"""
        c = cls._ai_cfg_load()
        if c["base"] and c["key"] and c["model"]:
            cls._ai_model = c["model"]
            return {"ok": True, "ready": True, "mode": "cloud", "model": c["model"],
                    "base": c["base"], "key": cls._mask_key(c["key"]), "has_key": True,
                    "reason": ""}
        ready, why, model = cls._ai_model_ready()
        return {"ok": True, "ready": ready, "mode": "local", "model": model or "",
                "base": "", "key": "", "has_key": bool(c["key"]), "reason": why}

    @classmethod
    def _ai_parse_reply(cls, txt, model):
        obj = None
        try:
            obj = json.loads(txt)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", txt)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except Exception:
                    obj = None
        if not isinstance(obj, dict):
            return None, "模型没返回 JSON: " + txt[:100]
        name = re.sub(r'[\\/:*?"<>|\s:]+', "", str(obj.get("name") or ""))[:40]
        desc = re.sub(r"\s+", " ", str(obj.get("desc") or "")).strip()[:120]
        if not name or not desc:
            return None, "模型返回内容不完整: " + txt[:100]
        return {"name": name, "desc": desc, "model": model,
                "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}, None

    @classmethod
    def _ai_describe(cls, rel):
        """出草稿：优先用页面里配好的云端视觉接口，没配才调本地 ollama。"""
        cfg = cls._ai_cloud()
        if cfg:
            return cls._ai_describe_cloud(rel, cfg)
        import urllib.request, urllib.error
        b64, err = cls._img_b64(rel)
        if err:
            return None, err
        prompt = AI_PROMPT
        payload = {
            "model": cls._ai_model or AI_MODEL, "prompt": prompt, "images": [b64], "stream": False,
            "think": False,          # qwen3.5 不关思考会返回空内容
            "format": "json", "options": {"temperature": 0.2, "num_predict": 120},
        }
        try:
            req = urllib.request.Request(
                OLLAMA_BASE + "/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
            txt = str(j.get("response") or "").strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:180]
            return None, "HTTP %s: %s" % (e.code, detail or e.reason)
        except Exception as e:
            return None, "调用本地模型失败: %s" % str(e)[:120]
        return cls._ai_parse_reply(txt, cls._ai_model or AI_MODEL)

    @classmethod
    def _ai_describe_cloud(cls, rel, cfg):
        """走 OpenAI 兼容的 /chat/completions 视觉接口（Key 存在本机 ai_config.json）。"""
        import urllib.request, urllib.error
        b64, err = cls._img_b64(rel)
        if err:
            return None, err
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": AI_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]}],
            "temperature": 0.2, "max_tokens": 300,
        }
        try:
            req = urllib.request.Request(
                cfg["base"] + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + cfg["key"]})
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:180]
            return None, "HTTP %s: %s" % (e.code, detail or e.reason)
        except Exception as e:
            return None, "调用云端视觉接口失败: %s" % str(e)[:120]
        try:
            txt = j["choices"][0]["message"]["content"]
        except Exception:
            return None, "返回格式不对: " + json.dumps(j, ensure_ascii=False)[:120]
        if isinstance(txt, list):   # 有些服务把 content 拆成多块
            txt = "".join(str(x.get("text") or "") for x in txt if isinstance(x, dict))
        return cls._ai_parse_reply(str(txt or "").strip(), cfg["model"])

    @classmethod
    def _ai_worker(cls, targets):
        drafts = cls._drafts_load()
        for rel in targets:
            with cls._ai_lock:
                if cls._ai_state["stop"]:
                    break
                cls._ai_state["current"] = rel
            draft, err = cls._ai_describe(rel)
            if draft:
                drafts[rel] = draft
                saved = True
                try:
                    cls._drafts_save(drafts)
                except Exception as e:
                    saved = False
                    drafts.pop(rel, None)
                    with cls._ai_lock:
                        cls._ai_state["errors"] = (
                            cls._ai_state["errors"]
                            + ["%s → 草稿写入失败: %s" % (rel, str(e)[:100])]
                        )[-12:]
                with cls._ai_lock:
                    # 写盘失败不能算成功，否则前端显示「草稿已存」但实际没有
                    cls._ai_state["done" if saved else "failed"] += 1
            else:
                with cls._ai_lock:
                    cls._ai_state["failed"] += 1
                    cls._ai_state["errors"] = (cls._ai_state["errors"] + ["%s → %s" % (rel, err)])[-12:]
        with cls._ai_lock:
            cls._ai_state.update({"running": False, "finished": time.time(), "current": ""})

    @classmethod
    def _ai_model_ready(cls):
        """本地 ollama 里挑一个能看图的模型；一个都没有就让人手动导入命名。"""
        import urllib.request
        try:
            with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=5) as r:
                j = json.loads(r.read().decode("utf-8", "ignore"))
            names = [str(m.get("name") or "") for m in (j.get("models") or [])]
        except Exception as e:
            return False, ("连不上本地 ollama（%s）：%s。可以点「配置 AI」填云端视觉接口的 Key，"
                           "或者先把 ollama 起来再一键识别。" % (OLLAMA_BASE, str(e)[:60])), ""
        # 优先用户明确的 qwen2.5vl，其次任意带视觉的本地模型
        pick = ""
        for want in ("qwen2.5vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl"):
            for n in names:
                if n.lower().split(":")[0] == want:
                    pick = n
                    break
            if pick:
                break
        if not pick:
            for n in names:
                low = n.lower()
                if "vl" in low or "llava" in low or "minicpm-v" in low:
                    pick = n
                    break
        if not pick:
            return False, ("本机没有能看图的模型（当前只有 %s）。可以 `ollama pull qwen2.5vl:3b`，"
                           "或者点「配置 AI」填云端视觉接口的 Key。"
                           % ("、".join(names) or "无")), ""
        cls._ai_model = pick
        return True, "", pick

    def _ai_scan_start(self):
        cfg = self._ai_cloud()
        if cfg:
            self._ai_model = cfg["model"]
        else:
            ready, why, _model = self._ai_model_ready()
            if not ready:
                return self._json(200, {"ok": False, "err": why, "need_config": True})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            req = {}
        targets = [str(t) for t in (req.get("targets") or []) if str(t).strip()]
        force = bool(req.get("force"))
        drafts = self._drafts_load()
        if not targets:
            recs = self._stickers()["records"]
            targets = [r["target"] for r in recs if r.get("scope") == "disk"
                       and r.get("kind") == "file" and r.get("exists")]
            if not force:
                targets = [t for t in targets if t not in drafts]
        # 只认 stickers/ 下真实存在的图片，避免任务里塞奇怪路径
        clean = []
        for t in targets:
            fp = os.path.normpath(os.path.join(HERE, t.replace("/", os.sep)))
            if fp.startswith(HERE) and os.path.isfile(fp) and fp.lower().endswith(IMG_EXTS):
                clean.append(t.replace("\\", "/"))
        try:
            limit = int(req.get("limit") or 0)
        except Exception:
            limit = 0
        if limit > 0:
            clean = clean[:limit]
        if not clean:
            return self._json(200, {"ok": False, "err": "没有需要识别的图片（可勾选「重识别」强制再跑）"})
        with self._ai_lock:
            if self._ai_state.get("running"):
                return self._json(200, {"ok": False, "err": "已有识别任务在跑，等它跑完或先停止"})
            self._ai_state.update({"running": True, "total": len(clean), "done": 0,
                                   "failed": 0, "skipped": 0, "current": "",
                                   "started": time.time(), "finished": 0, "stop": False,
                                   "errors": [], "model": self._ai_model or AI_MODEL})
        threading.Thread(target=self._ai_worker, args=(clean,), daemon=True).start()
        return self._json(200, {"ok": True, "total": len(clean),
                                "model": self._ai_model or AI_MODEL,
                                "note": "草稿仅供预填，必须人工核验才能入库"})

    def _ai_scan_status(self):
        with self._ai_lock:
            st = dict(self._ai_state)
        st["drafts"] = len(self._drafts_load())
        if st.get("running"):
            st["elapsed"] = int(time.time() - float(st.get("started") or time.time()))
        else:
            st["elapsed"] = int(float(st.get("finished") or 0) - float(st.get("started") or 0))
        return self._json(200, {"ok": True, **st})

    def _ai_scan_stop(self):
        with self._ai_lock:
            self._ai_state["stop"] = True
        return self._json(200, {"ok": True, "note": "已请求停止，当前这张跑完就停"})

    def _ai_config_save(self):
        """保存页面里填的云端视觉接口；key 留空 = 沿用已存的。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return self._json(400, {"err": "bad request body"})
        if req.get("clear"):
            try:
                os.remove(AI_CFG_PATH)
            except Exception:
                pass
            return self._json(200, dict(self._ai_status(), ok=True, cleared=True))
        cur = self._ai_cfg_load()
        base = str(req.get("base") or "").strip().rstrip("/")
        model = str(req.get("model") or "").strip()
        key = str(req.get("key") or "").strip() or cur["key"]
        if not (base and model and key):
            return self._json(200, {"ok": False, "err": "接口地址 / API Key / 模型名都要填"})
        if not re.match(r"^https?://", base):
            return self._json(200, {"ok": False, "err": "接口地址要以 http:// 或 https:// 开头"})
        self._ai_cfg_save({"base": base, "key": key, "model": model})
        self._ai_model = model
        return self._json(200, dict(self._ai_status(), ok=True, saved=True))

    def _provider_test(self):
        """实打实发一次最小请求，验证某个模型实例 / 接入源是否可用。"""
        import urllib.request, urllib.error
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            req = {}
        kind = "src" if req.get("kind") == "src" else "inst"
        pid = str(req.get("id") or "").strip()
        try:
            with io.open(CMD_CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            return self._json(500, {"ok": False, "err": "读取 cmd_config 失败: %s" % e})
        sources = {s.get("id", ""): s for s in (cfg.get("provider_sources") or [])}

        def key_of(src):
            k = src.get("key")
            return k[0] if isinstance(k, list) and k else (k if isinstance(k, str) else "")

        model = ""
        if kind == "inst":
            p = next((x for x in (cfg.get("provider") or []) if x.get("id") == pid), None)
            if not p:
                return self._json(404, {"ok": False, "err": "找不到模型实例 %s" % pid})
            model = p.get("model") or ""
            src = sources.get(p.get("provider_source_id") or "") or {}
        else:
            src = sources.get(pid) or {}
        if not src:
            return self._json(404, {"ok": False, "err": "找不到接入源 %s" % pid})
        base = (src.get("api_base") or "").rstrip("/")
        stype = (src.get("type") or "").lower()
        if not base:
            return self._json(200, {"ok": False, "err": "该源没有 API 地址（本地内置源，无需测试）"})
        if "chat" not in stype:
            return self._json(200, {"ok": True, "ms": 0,
                                    "reply": "", "note": "该源类型 %s，不做聊天测试" % (stype or "未知")})

        def do(url, payload, auth=True):
            hdr = {"Content-Type": "application/json"}
            if auth:
                k = key_of(src)
                if k:
                    hdr["Authorization"] = "Bearer " + k
                else:
                    return None, {"err": "该源没有配置 API key"}
            body = json.dumps(payload).encode("utf-8")
            t0 = time.time()
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(url, data=body, headers=hdr), timeout=45) as r:
                    txt = r.read().decode("utf-8", "ignore")
                return (int((time.time() - t0) * 1000), txt), None
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                return None, {"err": "HTTP %s: %s" % (e.code, detail or e.reason)}
            except Exception as e:
                return None, {"err": str(e)[:300]}

        if kind == "src" and not model:
            k = key_of(src)
            hdr = {"Authorization": "Bearer " + k} if k else {}
            t0 = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(base + "/models", headers=hdr),
                                            timeout=15) as r:
                    j = json.loads(r.read().decode("utf-8", "ignore"))
                ids = [m.get("id") for m in (j.get("data") or []) if m.get("id")]
                return self._json(200, {"ok": True, "ms": int((time.time() - t0) * 1000),
                                        "reply": "模型列表 %d 个" % len(ids),
                                        "models": ids[:12]})
            except urllib.error.HTTPError as e:
                return self._json(200, {"ok": False, "err": "HTTP %s: %s" % (
                    e.code, e.read().decode("utf-8", "ignore")[:250])})
            except Exception as e:
                return self._json(200, {"ok": False, "err": "连不上 %s：%s" % (base, str(e)[:200])})

        # 连通性按「用户提问」真实测一次：把用户的问题原样发给模型，能回答才算通
        question = str(req.get("question") or "").strip()[:300]
        if not question:
            question = "你好，请用一句话确认你能正常回答我的问题。"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": question}],
            "max_tokens": 160, "stream": False,
        }
        # ollama 的 OpenAI 兼容层：思考模型不关思考时 content 为空，测试会显示“无文本内容”
        if (src.get("provider") or "").lower() == "ollama" and \
                bool(p.get("ollama_disable_thinking", False)):
            payload["reasoning_effort"] = "none"
        for attempt in range(2):
            got, err = do(base + "/chat/completions", payload)
            if not err or "429" not in err.get("err", ""):
                break
            time.sleep(2)          # 限流（429）时等 2 秒重试一次
        if err:
            e = err.get("err", "请求失败")
            if "429" in e:
                e = "该模型当前被限流（HTTP 429），稍等十几秒再测一次即可；通路本身是通的。" + e
            return self._json(200, {"ok": False, "err": e, "model": model})
        ms, txt = got
        try:
            j = json.loads(txt)
            ch = (j.get("choices") or [{}])[0].get("message", {}) or {}
            reply = ch.get("content") or ""
            if not reply and ch.get("reasoning_content"):
                reply = "（推理模型已响应）"
            if not reply:
                reply = "（已响应，无文本内容）"
        except Exception:
            reply = txt[:120]
        return self._json(200, {"ok": True, "ms": ms, "model": model,
                                "question": question,
                                "reply": str(reply).strip()[:120]})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/provider/test":
            return self._provider_test()
        if p == "/api/access":
            # 旧页面用旧结构整文件覆盖 access.json，会丢掉中枢写入的 levels，
            # 因此只保留只读 GET，写入统一走中枢 /bot/tools/access。
            return self._json(410, {
                "err": "接入管理已迁移到中枢 /bot/tools/access，本接口不再接受写入",
                "hub": HUB_UI,
            })
        if p in ("/api/sticker/save", "/api/sticker/delete", "/api/sticker/add"):
            return self._sticker_op(p.rsplit("/", 1)[1])
        if p == "/api/sticker/upload":
            return self._sticker_upload()
        if p == "/api/sticker/ai_scan":
            return self._ai_scan_start()
        if p == "/api/sticker/ai_scan_stop":
            return self._ai_scan_stop()
        if p == "/api/sticker/ai_config":
            return self._ai_config_save()
        if p.startswith("/api/provider/"):
            segs = p.strip("/").split("/")          # api / provider / source|inst / op
            kind, op = segs[2], segs[3] if len(segs) > 3 else ""
            if kind in ("source", "inst") and op in ("add", "save", "delete", "default"):
                return self._provider_op(kind, op)
            return self._json(404, {"err": "not found"})
        if p in ("/api/persona/save", "/api/persona/delete", "/api/persona/add"):
            return self._persona_op(p.rsplit("/", 1)[1])
        if p == "/api/fallback":
            return self._fallback_save()
        if p != "/api/save":
            return self._json(404, {"err": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._json(400, {"err": "bad request body"})
        dir_, fname, fields = req.get("dir"), req.get("file"), req.get("fields") or {}
        if not dir_ or not fname or not isinstance(fields, dict):
            return self._json(400, {"err": "dir/file/fields required"})

        with io.open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        hit = None
        for r in data.get("imgs", []):
            if r.get("dir") == dir_ and r.get("file") == fname:
                hit = r
                break
        if hit is None:
            return self._json(404, {"err": "record not found: %s/%s" % (dir_, fname)})

        for k, v in fields.items():
            if k in ALLOW_FIELDS:
                hit[k] = str(v).strip()

        # 写回 JSON（滚动备份 .bak_edit）
        shutil.copyfile(JSON_PATH, JSON_PATH + ".bak_edit")
        with io.open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

        # 同步 map.txt（名称:意思:路径），路径匹配才动，其余行原样保留
        rel = (dir_ + "/" + fname).replace("\\", "/")
        lines = io.open(MAP_PATH, encoding="utf-8").read().splitlines()
        out, synced = [], False
        for ln in lines:
            if ln.startswith("#") or ln.strip() == "" or ":" not in ln:
                out.append(ln)
                continue
            parts = ln.split(":", 2)
            if len(parts) == 3 and parts[2].replace("\\", "/") == rel:
                out.append("%s:%s:%s" % (hit.get("name", parts[0]), hit.get("desc", parts[1]), parts[2]))
                synced = True
            else:
                out.append(ln)
        if synced:
            shutil.copyfile(MAP_PATH, MAP_PATH + ".bak_edit")
            with io.open(MAP_PATH, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
        return self._json(200, {"ok": True, "mapSynced": synced, "record": hit})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("serving http://127.0.0.1:%d/  (ctrl+c to stop)" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
