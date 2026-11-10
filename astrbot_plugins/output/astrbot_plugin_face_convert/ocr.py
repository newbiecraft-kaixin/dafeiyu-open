"""图片 OCR：RapidOCR 本地识别与 DirectML 加速。"""

import asyncio
import tempfile
import threading

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

_OCR_ENGINE = None  # RapidOCR 单例：首次用时初始化，模型随包自带、纯本地推理
_OCR_LOCK = threading.Lock()  # DirectML 并发推理会段错误（实测），初始化+推理全程串行


class OcrMixin:
    async def _ocr_image(self, event: AstrMessageEvent, comp) -> str | None:
        """图片 -> 图上文字（RapidOCR 本地识别，只提取文字，不做画面理解）。"""
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            return None
        with tempfile.TemporaryDirectory(prefix="ocr_") as tmpdir:
            path = await self._resolve_video(event, comp, tmpdir)
            if not path:
                return None
            # gif/webp 动图先转首帧 png：RapidOCR 读不了动图，别把整张图判成没字
            frames = self._vision_frames(path, tmpdir)
            if not frames:
                return None
            try:
                texts = await asyncio.to_thread(self._run_ocr_sync, frames[0])
            except Exception as e:
                logger.info(f"[face_convert] OCR 失败: {type(e).__name__}: {str(e)[:80]}")
                return None
            if not texts:
                return None
            text = " ".join(texts)[:400]
            logger.info(f"[face_convert] OCR 识别到 {len(texts)} 行: {text[:80]}")
            return text

    @staticmethod
    def _enable_gpu() -> bool:
        """让 RapidOCR 走 DirectML（DX12，Windows 免装 CUDA Toolkit，N/A/Intel 显卡通吃）"""
        try:
            import onnxruntime
            from rapidocr_onnxruntime import utils as _rutils

            if "DmlExecutionProvider" not in onnxruntime.get_available_providers():
                return False
            if getattr(_rutils.OrtInferSession, "_dml_patched", False):
                return True
            from onnxruntime import GraphOptimizationLevel, SessionOptions

            def _dml_init(self, config):
                sess_opt = SessionOptions()
                sess_opt.log_severity_level = 4
                sess_opt.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL
                self._verify_model(config["model_path"])
                self.session = onnxruntime.InferenceSession(
                    config["model_path"], sess_options=sess_opt,
                    providers=["DmlExecutionProvider", "CPUExecutionProvider"],
                )

            _rutils.OrtInferSession.__init__ = _dml_init
            _rutils.OrtInferSession._dml_patched = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] DirectML 加速启用失败，回落 CPU: {e}")
            return False

    @staticmethod
    def _run_ocr_sync(path: str) -> list[str]:
        """同步 OCR，放线程池跑，避免卡事件循环。兼容新旧 RapidOCR 两种返回格式。
        全程持锁：DirectML 下并发推理会触发原生段错误，宁可排队也不并发。"""
        global _OCR_ENGINE
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                gpu_on = False
                try:
                    from rapidocr_onnxruntime import RapidOCR
                    gpu_on = OcrMixin._enable_gpu()
                    _OCR_ENGINE = RapidOCR()
                except ImportError:
                    from rapidocr import RapidOCR
                    _OCR_ENGINE = RapidOCR()
                logger.info(
                    f"[face_convert] RapidOCR 引擎初始化完成（{'DirectML GPU 加速' if gpu_on else 'CPU'}）"
                )
            res = _OCR_ENGINE(path)
        texts: list[str] = []
        if isinstance(res, tuple):  # 1.x 经典 API: (result, elapse)
            for it in (res[0] or []):
                if len(it) >= 2 and it[1]:
                    texts.append(str(it[1]))
        else:  # 2.x/3.x: RapidOCROutput
            texts = [str(t) for t in (getattr(res, "txts", None) or [])]
        return texts
