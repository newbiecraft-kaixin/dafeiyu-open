"""语音 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import asyncio
import os
import time


from astrbot.api.event import AstrMessageEvent, filter

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class VoiceMixin:
    @filter.llm_tool(name="send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str = "", mood: str = ""):
        """把一段话转成语音消息发到当前会话（本地 CosyVoice2 克隆声线，御姐音、自然不生硬）。**只有对方明确要求语音时才用**：说了"用语音说/发条语音/语音回我/说给我听/念给我听"才发。对方没提语音就一律用文字，不要自作主张发语音。内容不超过 200 字。念的内容要像日常说话，多带语气词（呀、啦、嘛、呢），别用书面语、别带表情符号和链接。

        Args:
            text(string): 要念出来的内容
            mood(string): 语气，可选：乖巧、开心、撒娇、委屈、认真、困了、惊讶、生气、害羞。留空=乖巧
        """
        text = (text or "").strip()
        if not text:
            return "要念什么呀，内容不能为空"
        if (denied := _daily_gate("发语音", 20)):
            return denied
        text = text[:200]
        ve = _load_voice_engine()
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return "语音合成组件未安装（edge-tts 缺失）"
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            mood = (mood or "").strip() or "乖巧"
            parts = _split_voice(text)
            sent = 0
            for i, part in enumerate(parts):
                out = os.path.join(
                    VOICE_DIR, f"voice_{int(time.time() * 1000)}_{i}.mp3"
                )
                try:
                    if ve is not None:
                        # 本地 CosyVoice：克隆御姐声线 + 按情绪调语速
                        await ve.synth(part, out, voice=ve.EDGE_VOICE, mood=mood)
                    else:
                        await edge_tts.Communicate(
                            part, "zh-CN-XiaoxiaoNeural", rate="-6%", pitch="-12Hz"
                        ).save(out)
                except Exception as e:  # noqa: BLE001
                    # CosyVoice 挂了也别把这条消息吞掉：记日志，走文字兜底
                    logger.warning(
                        f"[basic_tools] 语音合成失败（第 {i + 1}/{len(parts)} 段）: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue
                if not os.path.exists(out) or os.path.getsize(out) < 1000:
                    continue
                await self._push_segments(
                    event,
                    [{"type": "record", "data": {"file": "file:///" + out.replace("\\", "/")}}],
                )
                sent += 1
                if i + 1 < len(parts):
                    # 两条语音之间留一点「松手再按」的空隙，别连着怼出来
                    await asyncio.sleep(0.7)
            if sent == 0:
                # 一段都没合成出来：别让这条消息凭空消失，把要念的内容按文字发出去
                try:
                    await self._push_segments(
                        event, [{"type": "text", "data": {"text": text}}]
                    )
                    self._queue_memo(event.unified_msg_origin, text)
                    return "语音合成没成功，已经把内容改成文字发出来了"
                except Exception as e:  # noqa: BLE001
                    return f"语音生成失败了，文字兜底也没发出去: {e}"
            # 语音已经把内容说完了，别再让模型补一条同样意思的文字
            event._fy_voice_sent = True
            # 语音是直发的，管道文字链会被清空：把念出的文本同步推成桌宠字幕
            event._fy_voice_text = "".join(parts)
            push_pet_subtitle(event, event._fy_voice_text)
            self._queue_memo(event.unified_msg_origin, "".join(parts))
            # 留最近 100 个，防目录膨胀（更精细的清理交给每日维护）
            olds = sorted(
                (f for f in os.listdir(VOICE_DIR) if f.endswith(".mp3")),
                key=lambda f: os.path.getmtime(os.path.join(VOICE_DIR, f)),
            )
            for old in olds[:-VOICE_KEEP_FILES]:
                try:
                    os.remove(os.path.join(VOICE_DIR, old))
                except OSError:
                    pass
            return f"语音已经发出去啦（语气：{mood}，{sent} 条）"
        except Exception as e:  # noqa: BLE001
            return f"发语音失败: {e}"

    @filter.llm_tool(name="switch_voice")
    async def switch_voice(self, event: AstrMessageEvent, name: str = ""):
        """切换自己的说话声线，说话和唱歌都会一起换成新声线（全局生效，下一条语音就用新的）。**只有主人明确要求时才用**：说了"换个声音/换成姬子声线/用流萤的声音说话"才切换；不要自己随便换声线。不知道有哪些声线时，把 name 留空先查一下。

        Args:
            name(string): 声线名称，例如 姬子、小忆、流萤、御姐，或热门音色 晓晓、晓伊、云希；留空=返回当前声线和全部可选声线
        """
        ve = _load_voice_engine()
        if ve is None:
            return "语音引擎没加载起来，暂时换不了声线"
        if not self._is_admin(event):
            return "换声线这个事只有主人能决定哦"
        raw = (name or "").strip()
        profiles = ve.list_profiles()
        if not raw:
            cur = ve.active_profile()
            rows = [
                f"{n}{('（' + m + '）') if m else ''}{' ← 当前' if n == cur else ''}"
                for n, m in profiles
            ]
            if not rows:
                return "还没有声线档案"
            return f"当前声线：{cur}。可选声线：\n" + "\n".join(rows)
        if not ve.set_profile(raw):
            names = "、".join(n for n, _ in profiles) or "无"
            return f"没有「{raw}」这个声线。可选：{names}"
        return f"声线已经换成「{raw}」了，说话和唱歌都会用它（下一条语音生效）"

    @filter.command("声线")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_voice_profiles(self, event: AstrMessageEvent, name: str = ""):
        """/声线 [名称] —— 不带名称列出声线，带名称切换（说话和唱歌一起换）"""
        ve = _load_voice_engine()
        if ve is None:
            await _hint_private(event, "语音引擎没加载起来。")
            event.stop_event()
            return
        raw = (name or "").strip()
        if not raw:
            cur = ve.active_profile()
            rows = [
                f"{n}{('（' + m + '）') if m else ''}{' ← 当前' if n == cur else ''}"
                for n, m in ve.list_profiles()
            ]
            if not rows:
                await _hint_private(event, "还没有声线档案。")
                event.stop_event()
                return
            await _hint_private(event, "可用声线：\n" + "\n".join(rows)
                                + "\n发 /声线 名称 切换。")
            event.stop_event()
            return
        if not ve.set_profile(raw):
            await _hint_private(event, f"没有「{raw}」这个声线。")
            event.stop_event()
            return
        await _hint_private(event, f"声线换成「{raw}」了，说话和唱歌都会用它。")
        event.stop_event()


exit_plugin_main(globals(), _REAL_MODULE_NAME)
