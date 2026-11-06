"""翻唱（封面） mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import os
import time


from astrbot.api import logger

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class CoverMixin:
    async def _cover_send_bg(self, bot, action: str, params: dict,
                             name: str, src: str, umo: str = "",
                             at_seg: dict | None = None) -> None:
        """后台翻唱：整首分轨 → 按干声挑高潮段 → 只转换这一段 → 混伴奏发回。

        工具调用有 60 秒超时，整首分轨加转换要几分钟，所以必须放后台跑。
        开始那句等待提示不在这里发——工具自己会立刻返回一句，让模型正常回，
        免得同一次请求既直发又由模型回，用户收到两条一样的「开始翻唱」。
        """
        async def _send(segs: list[dict], memo: str = "",
                        mention: bool = False) -> None:
            if mention and at_seg:
                segs = [at_seg] + segs
            try:
                await bot.api.call_action(
                    action, **{**params, "message": segs})
                # 成品绕过了 pipeline，补一句进会话，免得下一轮它以为没发
                self._queue_memo(umo, memo)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[basic_tools] 后台翻唱发送失败: "
                               f"{type(e).__name__}: {e}")

        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            out = os.path.join(VOICE_DIR,
                               f"cover_{int(time.time() * 1000)}.mp3")
            info = await _run_cover_job(
                {"mode": "cover", "src": src, "out": out, "semi": 0})
            dur = float(info.get("duration") or 0) or _audio_seconds(out)
            if not info.get("ok") or not os.path.exists(out) \
                    or os.path.getsize(out) < 2000:
                fail = f"《{name}》这次没翻唱出来，再点一次？"
                await _send([{"type": "text", "data": {"text": fail}}], fail,
                            mention=True)
                return
            _sing_cache_save(_cover_cache_key(name, src), out)
            # cover_render 已经按干声挑好高潮段并只转换这一段，这里直接发
            hook_start = float(info.get("hook_start") or 0)
            file_url = "file:///" + out.replace("\\", "/")
            await _send([{"type": "record", "data": {"file": file_url}}],
                        "sing_song function cover action has finish")
            logger.info(f"[basic_tools] 后台翻唱完成：《{name}》{dur:.0f} 秒"
                        + (f"（原曲第 {hook_start:.0f} 秒起）" if hook_start else ""))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 后台翻唱异常: "
                           f"{type(e).__name__}: {e}")
            err_text = f"《{name}》翻唱出错了（{str(e)[:50]}），再试一次？"
            await _send([{"type": "text", "data": {"text": err_text}}], err_text,
                        mention=True)
        finally:
            _cover_unlock()


exit_plugin_main(globals(), _REAL_MODULE_NAME)
