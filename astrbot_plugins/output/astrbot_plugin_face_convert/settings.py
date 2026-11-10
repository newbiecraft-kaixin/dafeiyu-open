"""表情字典与运行配置装载。"""

import asyncio
import json
import os
import re

from astrbot.api import logger

from .utils import PLUGIN_DIR

DEFAULT_CONFIG = {
    "hedge_eraser": True,
    "face_cooldown_minutes": 10,
    "max_media_mb": 80,
    "video_timeout_s": 300,
    "video_max_duration_s": 300,
    "video_max_frames": 16,
    "video_audio_chunk_s": 60,
    "video_workers": 2,
    "vl_timeout_s": 60,
    "asr_timeout_s": 60,
    "max_concurrent_media": 1,
    "media_queue_timeout_s": 10,
    # 语音群里不 @ 也一律解析；视频默认不解析（抽帧+视觉模型费算力、价值低）
    "video_enabled": False,
    # 太长的语音不听
    "voice_max_duration_s": 60,
    "video_skip_over_s": 90,
}


class SettingsMixin:
    def _load_face_dictionaries(self):
        map_path = os.path.join(PLUGIN_DIR, "face_map.json")
        with open(map_path, encoding="utf-8") as f:
            self.face_map: dict[str, int] = json.load(f)
        # 反向表：id -> 名字（用于把收到的表情翻译成文字喂给 LLM）
        self.id2name: dict[int, str] = {}
        for name, fid in self.face_map.items():
            self.id2name.setdefault(fid, name)
        # 手工优先级：通称覆盖
        self.id2name[277] = "旺柴"
        self.id2name[179] = "doge"
        # 再并入表情包索引 map.txt 的黄脸表（275 个），查不到 ID 时尽量给名字
        try:
            _stickers_map = os.path.join(
                os.path.dirname(PLUGIN_DIR), "..", "stickers", "map.txt"
            )
            with open(_stickers_map, encoding="utf-8") as f:
                for _line in f:
                    _parts = _line.strip().split(":")
                    if len(_parts) >= 4 and _parts[2] == "face":
                        try:
                            self.id2name.setdefault(
                                int(_parts[3]), _parts[0]
                            )
                        except ValueError:
                            pass
        except OSError:
            pass
        self.pattern = re.compile(r"\[([^\[\]]{1,8})\]")

    def _load_runtime_config(self):
        # 轻量配置：同目录 config.json 可覆盖（缺省即默认值）
        self.cfg = DEFAULT_CONFIG.copy()
        try:
            with open(os.path.join(PLUGIN_DIR, "config.json"), encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.cfg.update({k: loaded[k] for k in loaded if k in self.cfg})
        except (OSError, ValueError):
            pass
        # 配置错误不能把队列锁死或产生无上限的媒体任务。
        for key, low, high in (
            ("max_media_mb", 1, 200), ("video_timeout_s", 10, 900),
            ("video_max_duration_s", 1, 300), ("video_max_frames", 1, 32),
            ("video_audio_chunk_s", 10, 60), ("video_workers", 1, 2),
            ("vl_timeout_s", 1, 120), ("asr_timeout_s", 1, 120),
            ("max_concurrent_media", 1, 4), ("media_queue_timeout_s", 1, 60),
        ):
            try:
                self.cfg[key] = min(high, max(low, int(self.cfg[key])))
            except (TypeError, ValueError, OverflowError):
                self.cfg[key] = DEFAULT_CONFIG[key]
        self._media_sem = asyncio.Semaphore(
            max(1, int(self.cfg.get("max_concurrent_media", 2)))
        )
        # 裸视频链接的后台解析任务（抓取+转写+总结），防被 GC 提前回收
        self._video_tasks: set[asyncio.Task] = set()
        # 表情冷却：按会话记录她最近用过的脸 [(脸id, 时间戳)]（治"又发好多狗头"；
        # 早期版本是全局共享列表，会跨群串扰，已改为按会话隔离 + 时间过期）
        self._face_history: dict[str, list[tuple[int, float]]] = {}
        # DashScope key（用于语音转写）+ 管理员名单（群聊里标成「主人」）
        self.dashscope_key = ""
        self._whisper = None  # 本地 faster-whisper 懒加载单例（免费 ASR）
        self.admins: list[str] = []
        self._admins_mtime: float = 0.0
        # 控制台「管理员身份」标签（主人/爸爸/母亲大人…）：services/hub/data/admins.json
        self.admin_identities: dict[str, str] = {}
        self._profiles_mtime: float = 0.0
        cfg: dict = {}
        try:
            cfg_path = os.path.abspath(
                os.path.join(PLUGIN_DIR, "..", "..", "cmd_config.json")
            )
            with open(cfg_path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            for prov in cfg.get("provider", []):
                if prov.get("id") == "dashscope_tts" and prov.get("api_key"):
                    self.dashscope_key = prov["api_key"]
                    break
            self.admins = [str(a).strip() for a in (cfg.get("admins_id") or [])]
            self._admins_mtime = os.path.getmtime(cfg_path)
            self._load_admin_identities(os.path.dirname(cfg_path))
        except Exception as e:
            logger.warning(f"[face_convert] 读取 DashScope key 失败: {e}")
        # 本地视觉模型（视频抽帧描述用）：Ollama qwen2.5vl:3b，3.2G 显存，和姬子语音抢显存没那么狠
        self.ollama_base = "http://127.0.0.1:11434"
        self.ollama_vision_model = "qwen2.5vl:3b"
        try:
            for prov in cfg.get("provider", []):
                if str(prov.get("id", "")).startswith("ollama-chat"):
                    self.ollama_base = str(
                        prov.get("api_base") or self.ollama_base
                    ).rstrip("/")
                    mc = prov.get("model_config") or {}
                    self.ollama_vision_model = mc.get("model") or self.ollama_vision_model
                    break
        except Exception:  # noqa: BLE001
            pass
        # 音频字幕系统（services/subtitle，23405）：常驻进程里缓存 whisper 模型，
        # 比每次在插件线程池冷启动快；服务没起来就回退本地实现。
        self.subtitle_url = os.environ.get(
            "SUBTITLE_URL", "http://127.0.0.1:23405"
        ).rstrip("/")
        logger.info(
            f"[face_convert] 表情字典载入完成，共 {len(self.face_map)} 条；"
            f"语音=本地whisper{' + 云端兜底' if self.dashscope_key else '（无云端key）'}，群里不@也解析；"
            f"视频={'解析' if self.cfg.get('video_enabled') else '不解析'}"
        )

    def _load_admin_identities(self, data_dir: str) -> None:
        """读控制台管理员身份标签：data 同根的 services/hub/data/admins.json。

        data_dir 是 AstrBot 的 data 目录；FlatFish 根 = data 上溯 3 层
        （data -> AstrBot-* -> astrbot-win -> FlatFish）。
        """
        try:
            root = os.path.dirname(os.path.dirname(os.path.dirname(data_dir)))
            profiles_path = os.path.join(
                root, "services", "hub", "data", "admins.json"
            )
            self._profiles_mtime = os.path.getmtime(profiles_path)
            with open(profiles_path, encoding="utf-8-sig") as f:
                data = json.load(f)
            rows = data.get("profiles") if isinstance(data, dict) else None
            out: dict[str, str] = {}
            for uid, item in (rows or {}).items():
                ident = ""
                if isinstance(item, dict):
                    ident = str(item.get("identity") or "").strip()
                if ident:
                    out[str(uid).strip()] = ident[:24]
            self.admin_identities = out
        except (OSError, ValueError) as e:
            logger.debug(f"[face_convert] 读取管理员身份标签失败: {e}")
            self.admin_identities = {}

    def _refresh_admins(self) -> None:
        """管理员名单与身份标签动态跟手：mtime 变了就重读对应文件。

        管理面板增删管理员/改身份标签后不用重启插件（ speaker_tag / group_window 同口径）。
        """
        try:
            cfg_path = os.path.abspath(
                os.path.join(PLUGIN_DIR, "..", "..", "cmd_config.json")
            )
            try:
                mtime = os.path.getmtime(cfg_path)
            except OSError:
                return
            pmtime = self._profiles_mtime
            if pmtime or self.admin_identities:
                # 身份标签文件跟 cmd_config 同根，跟着一起盯 mtime
                data_dir = os.path.dirname(cfg_path)
                root = os.path.dirname(os.path.dirname(os.path.dirname(data_dir)))
                try:
                    pmtime = os.path.getmtime(
                        os.path.join(root, "services", "hub", "data", "admins.json")
                    )
                except OSError:
                    pmtime = 0.0
            if self.admins and mtime == self._admins_mtime and pmtime == self._profiles_mtime:
                return
            with open(cfg_path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            self.admins = [str(a).strip() for a in (cfg.get("admins_id") or [])]
            self._admins_mtime = mtime
            if pmtime != self._profiles_mtime:
                self._load_admin_identities(os.path.dirname(cfg_path))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] 刷新管理员名单失败: {e}")
