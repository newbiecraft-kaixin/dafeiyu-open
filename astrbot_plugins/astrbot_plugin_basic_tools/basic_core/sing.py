"""唱歌 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .compat import enter_plugin_main, exit_plugin_main, install_helpers

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class SingMixin:
    @filter.llm_tool(name="sing_song")
    async def sing_song(self, event: AstrMessageEvent, song: str = "",
                        lyrics: str = "", cover: bool = False):
        """唱歌：真实歌曲默认用 Seed-VC 翻唱（效果最好），只发高潮段；儿歌/纯旋律才现编词唱。
        注意：这不是放歌曲文件，也不是把歌词念出来，是按音高真唱。
        内置旋律：小星星、两只老虎、生日歌、欢乐颂；
        真实歌名只要曲库里有原曲，就自动走翻唱（保留原唱旋律节奏咬字、只换成聊天语音同款音色，
        也就是肥鱼自己说话的那条嗓子，不是模仿原唱歌手），
        不用用户特别说「翻唱」。曲库没有的歌直接回「没有《歌名》这首歌」，
        不要联网下载，也不要问用户同不同意（下载原曲由主人用 /扒歌 完成）。
        对方说「唱首歌/来一个/唱歌给我听」就用这个；只想要原曲文件请让主人用 /点歌。

        Args:
            song(string): 旋律名或歌名；留空用内置旋律
            lyrics(string): 想唱的主题（如"唱给主人过生日"）；也可以直接给成品歌词，每句用 | 分隔。
                内置旋律对字数有要求（小星星每句 7 字），给成品词时每句字数要对上，多出的字会被裁掉
            cover(boolean): 保留兼容；歌名在曲库有原曲时会自动翻唱，一般不用手动传。
                不用特意传 cover，说「翻唱」也走同一条路
                lyrics 留空=用原词原调翻唱；给了改编歌词=按原曲旋律唱改编词再换音色
                唱真实歌曲时 lyrics 一律留空，不要自己编词填进来
                曲库里没有这首歌时不会自动下载，只回一句「没有《歌名》这首歌」；
                下载由主人用 /扒歌 完成，不要调 search_song / fetch_song
        """
        ret = await self._sing_song_impl(event, song=song, lyrics=lyrics, cover=cover)
        if ret.startswith("sing_song function "):
            # 音频已经发出去：只回标记，别让模型补「唱完了/上次那版」这类话
            return ret
        if ret.startswith("（"):
            # 括号开头的是给模型看的内部说明（提示已经直发过了），别再发给用户
            setattr(event, "_fy_sing_prompt", True)
            return ret
        try:
            await self._notify_at(event, ret)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 唱歌提示发送失败: {type(e).__name__}: {e}")
            return ret
        setattr(event, "_fy_sing_prompt", True)
        return "（提示已经 @ 用户发过去了；你不要再重复这句话，也别补唱歌说明）"

    async def _sing_song_impl(self, event: AstrMessageEvent, song: str = "",
                              lyrics: str = "", cover: bool = False):
        if (denied := _daily_gate("唱歌", 30)):
            return denied

        eng = _load_sing_engine()
        name = re.sub(r"[《》\"'“”]", "", str(song or "")).strip()
        has_lyrics = bool(str(lyrics or "").strip())
        # 真实歌名（不是内置儿歌）且曲库有原曲 → 默认直接翻唱，别退回 TTS 凑
        if not cover and not has_lyrics and name and _find_music_file(name) \
                and (eng is None or eng.find_score(name) is None):
            cover = True

        # 原曲整首翻唱：Demucs 分人声 → Seed-VC 换音色 → 混原伴奏
        if cover and not has_lyrics:
            if not name:
                return "翻唱要告诉我歌名，比如「翻唱 平凡之路」"
            src = _find_music_file(name)
            if not src:
                # 资源库没有就直接回没有这首歌，别擅自联网下载
                return f"没有《{name}》这首歌。"
            bot = getattr(event, "bot", None)
            if bot is None:
                return "当前环境拿不到 QQ 客户端"
            ckey = _cover_cache_key(name, src)
            hit = _sing_cache_find(ckey)
            if hit:
                dur = _audio_seconds(hit)
                file_url = "file:///" + hit.replace("\\", "/")
                segs = ([{"type": "file",
                          "data": {"file": file_url, "name": f"{name}.mp3"}}]
                        if dur > 58 else
                        [{"type": "record", "data": {"file": file_url}}])
                try:
                    await self._push_segments(event, segs)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[basic_tools] 缓存翻唱发送失败: "
                                   f"{type(e).__name__}: {e}")
                    return (f"缓存里《{name}》找到了，但发不出去"
                            f"（{str(e)[:60]}）")
                setattr(event, "_fy_sing_sent", True)
                self._queue_memo(event.unified_msg_origin,
                                 "sing_song function cover action has finish")
                return "sing_song function cover action has finish"
            if not _cover_lock():
                # 工具返回的字符串只到模型手里，模型经常不回这句，用户就以为没反应
                # 这里直接发到会话，再让模型闭嘴，保证拒绝一定有反馈
                try:
                    age = int(time.time() - os.path.getmtime(COVER_LOCK))
                except OSError:
                    age = 0
                busy = "上一首还在翻唱"
                if 0 < age < COVER_LOCK_TTL:
                    busy += f"（已经跑了 {age} 秒）"
                busy += "，等它发出来再点下一首"
                try:
                    # 群聊要 @ 一下点歌的人，保证拒绝一定有反馈
                    await self._notify_at(event, busy)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[basic_tools] 翻唱拒绝提示发送失败: "
                                   f"{type(e).__name__}: {e}")
                return "（已经直接告诉用户上一首还在翻唱，你不要再重复这句话）"
            try:
                action, params = self._send_target(event)
            except Exception as e:  # noqa: BLE001
                _cover_unlock()
                return str(e)[:80]
            # 整首要跑 Demucs + Seed-VC，几分钟，超过工具 60 秒超时：
            # 放后台跑，好了直接把高潮段发到当前会话
            umo = event.unified_msg_origin
            task = asyncio.create_task(
                self._cover_send_bg(bot, action, params, name, src, umo,
                                    self._at_seg(event)))
            _COVER_TASKS.add(task)
            task.add_done_callback(_COVER_TASKS.discard)
            # 这里只回一句等待提示，别再额外直发一条（会跟模型的回复重复）
            return f"《{name}》翻唱已开工，请等 1~2 分钟，成品我直接发出来"

        if eng is None:
            return "唱歌引擎没加载起来（sing_engine.py 缺失）"
        sc = eng.find_score(song)
        if sc is None and name:
            # 资源库/缓存里都没有这首：直接回没有这首歌
            if not _score_cache_find(name) and not _find_music_file(name):
                return f"没有《{name}》这首歌。"
            sc = await self._melody_score(song)
        if sc is None:
            sc = eng.pick_score("")
        if sc.get("extracted"):
            patterns = list(sc["patterns"])
        else:
            patterns, _ = eng.score_summary(sc)
        raw = (lyrics or "").strip()
        lines = None
        if "|" in raw:
            lines = eng.normalize_lyrics(raw.split("|"), patterns)
            if lines is None:
                # 行数/字数对不上也别丢：原词按旋律字数切开，宁可重复也别重编怪词
                lines = eng.fit_lyrics(raw.split("|"), patterns)
        if lines is None:
            lines = await self._fill_sing_lyrics(event, sc, patterns, raw)
        if lines is None:
            lines = eng.normalize_lyrics(sc["demo"].split("|"), patterns)
        if lines is None:
            return "这次词没编顺，先不唱了"

        if sc.get("extracted"):
            job = {
                "song": sc["name"],
                # 扒谱已量化成拍，bpm 用扒出来估的；内置老缓存没 bpm 时按 96
                "bpm": float(sc.get("bpm") or 96),
                "lines": [
                    {
                        "text": text,
                        "notes": notes,
                        "beats": sum(float(d) for _, d in notes),
                    }
                    for notes, text in zip(sc["lines"], lines)
                ],
            }
        else:
            job = eng.build_job(sc, lines)
        job["mood"] = sc.get("mood") or "开心"
        job["voice"] = SING_VOICE
        if SING_VOICE == "cosy":
            # 唱歌也跟 /声线：带上当前声线的参考音频和转写文本，服务端按请求换声线。
            # 顺带让下面的缓存键天然带上声线，切了声线不会命中旧音色的成品。
            _ve = _load_voice_engine()
            if _ve is not None:
                job["prompt_wav"] = _ve.profile_ref()
                job["prompt_text"] = _ve.profile_prompt()
        # 同一套 job（歌名+歌词+音色+谱面）3 天内重复点，直接复用上次成品
        cache_key = _sing_cache_key(
            {"kind": "cover" if cover else "tts",
             **{k: v for k, v in job.items() if k != "out"}})
        cached = _sing_cache_find(cache_key)
        if cached:
            out = cached
            info = {"ok": True, "duration": _audio_seconds(out)}
            logger.info(f"[basic_tools] 唱歌缓存命中：{sc['name']}")
        else:
            os.makedirs(VOICE_DIR, exist_ok=True)
            out = os.path.join(VOICE_DIR, f"sing_{int(time.time() * 1000)}.mp3")
            job["out"] = out
            job_path = out + ".json"
            stdout = b""
            stderr = b""
            try:
                with open(job_path, "w", encoding="utf-8") as f:
                    json.dump(job, f, ensure_ascii=False)
                renderer = os.path.join(PLUGIN_DIR, "sing_render.py")
                res = await _run_child([sys.executable, renderer, job_path], 300)
                if res is None:
                    return "这次没唱出来（渲染超时）"
                stdout, stderr = res
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[basic_tools] 唱歌渲染失败: {type(e).__name__}: {e}")
                return f"这次没唱出来（渲染 {type(e).__name__}）"
            finally:
                try:
                    os.remove(job_path)
                except OSError:
                    pass

            info: dict = {}
            for ln in reversed((stdout or b"").decode("utf-8", "ignore").splitlines()):
                ln = ln.strip()
                if ln.startswith("{"):
                    try:
                        info = json.loads(ln)
                    except ValueError:
                        info = {}
                    break
            if not info.get("ok") or not os.path.exists(out) \
                    or os.path.getsize(out) < 2000:
                logger.warning(
                    f"[basic_tools] 唱歌没出结果: {info} {(stderr or b'')[-200:]!r}"
                )
                return "这次没唱出来，再点一次？"
            if cover:
                # 改编歌词：上面唱出来的是干声，再过 Seed-VC 换成聊天语音同款音色（说话嗓子）
                conv = os.path.join(VOICE_DIR,
                                    f"cover_{int(time.time() * 1000)}.mp3")
                cinfo = await _run_cover_job(
                    {"mode": "post", "src": out, "out": conv, "semi": 6})
                if cinfo.get("ok") and os.path.exists(conv) \
                        and os.path.getsize(conv) > 2000:
                    try:
                        os.remove(out)
                    except OSError:
                        pass
                    out = conv
            _sing_cache_save(cache_key, out)
        dur = float(info.get("duration") or 0) or _audio_seconds(out)
        file_url = "file:///" + out.replace("\\", "/")
        try:
            if dur > 58:
                # 太长的唱歌音频没法塞进语音条，只能发文件
                await self._push_segments(
                    event,
                    [{"type": "file", "data": {"file": file_url, "name": f"{sc['name']}.mp3"}}],
                )
            else:
                await self._push_segments(
                    event, [{"type": "record", "data": {"file": file_url}}]
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 唱歌音频发送失败: {type(e).__name__}: {e}")
            return f"唱是唱出来了，但发不出去（{str(e)[:60]}）"
        setattr(event, "_fy_sing_sent", True)
        self._queue_memo(event.unified_msg_origin,
                         "sing_song function sing action has finish")
        # 只留最近 100 首，防目录膨胀
        try:
            olds = sorted(
                (f for f in os.listdir(VOICE_DIR)
                 if f.startswith(("sing_", "cover_")) and f.endswith(".mp3")),
                key=lambda f: os.path.getmtime(os.path.join(VOICE_DIR, f)),
            )
            for old in olds[:-VOICE_KEEP_FILES]:
                os.remove(os.path.join(VOICE_DIR, old))
        except OSError:
            pass
        return "sing_song function sing action has finish"

    async def _fill_sing_lyrics(self, event, sc, patterns, theme):
        """按每句字数让模型填词；不合格重试一次，还不行返回 None（调用方退回内置词）。"""
        eng = _load_sing_engine()
        if eng is None:
            return None
        counts = "、".join(str(n) for n in patterns)
        example = "|".join("啊" * n for n in patterns)
        prompt = (
            f"给旋律《{sc['name']}》填歌词：一共 {len(patterns)} 句，每句汉字数必须是 {counts}。"
            + (f"主题：{theme}。" if theme else "主题：轻松好玩，像唱给群友听的。")
            + "\n只输出一行：每句用 | 分隔，只写汉字，不要标点、序号、引号、解释。"
            + "\n要像能唱出来的歌词：用口语里常见的词，一句说一件事；别用「策略/优化/变现/赋能」这类书面词和生僻搭配，别堆同一个字。"
            + f"\n格式示例：{example}"
        )
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        for _ in range(2):
            try:
                prov = self.context.get_using_provider(umo)
                resp = await prov.text_chat(
                    prompt=prompt,
                    system_prompt="你只输出用 | 分隔的歌词，不解释，每句汉字数严格按要求。",
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[basic_tools] 填词失败: {type(e).__name__}: {e}")
                return None
            text = re.sub(r"[，。！？、；：,.!?]", "", text)
            lines = eng.normalize_lyrics(re.split(r"[|\n]", text), patterns)
            if lines:
                return lines
            prompt += "\n刚才字数不对，这次严格按每句字数重写。"
        return None

    async def _melody_score(self, name: str, force: bool = False):
        """取可唱乐谱：先查扒谱缓存，没有再从曲库/网易云 mp3 现场扒（成功写缓存）。

        force=True 跳过缓存重扒（/扒谱 用来刷新）。返回 None = 没谱。
        谱里的音符时长已量化成「拍」，配 bpm 一起用。
        """
        name = re.sub(r"[《》\"'“”]", "", str(name or "")).strip()
        if not name:
            return None
        if not force:
            hit = _score_cache_find(name)
            if hit:
                logger.info(f"[basic_tools] 用缓存谱《{hit.get('name') or name}》")
                return self._score_from_cache(hit)
        path = _find_music_file(name)
        if not path:
            path = await _netease_fetch_song(name)
        if not path or not os.path.exists(path):
            return None
        script = os.path.join(PLUGIN_DIR, "melody_extract.py")
        if not os.path.exists(script):
            return None
        try:
            res = await _run_child([sys.executable, script, path], 120)
            if res is None:
                return None
            stdout, stderr = res
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 扒旋律失败: {type(e).__name__}: {e}")
            return None
        info: dict = {}
        for ln in reversed((stdout or b"").decode("utf-8", "ignore").splitlines()):
            if ln.strip().startswith("{"):
                try:
                    info = json.loads(ln)
                except ValueError:
                    info = {}
                break
        if not info.get("ok") or not info.get("lines"):
            logger.warning(
                f"[basic_tools] 扒旋律没结果: {info.get('error')} {(stderr or b'')[-160:]!r}"
            )
            return None
        patterns = [int(n) for n in (info.get("syllables") or [])][: len(info["lines"])]
        lines = info["lines"][: len(patterns)]
        keep = [(ln, n) for ln, n in zip(lines, patterns) if n > 0]
        lines = [ln for ln, _ in keep]
        patterns = [n for _, n in keep]
        if not patterns or sum(patterns) < 4:
            return None
        info["lines"], info["syllables"] = lines, patterns
        song_name = os.path.splitext(os.path.basename(path))[0]
        _score_cache_save(song_name, info, path)
        logger.info(
            f"[basic_tools] 从《{song_name}》扒到旋律："
            f"{len(patterns)} 句 / {sum(patterns)} 音 / bpm {info.get('bpm')} / {info.get('total')} 秒"
        )
        return self._score_from_cache(info, name=song_name)

    @staticmethod
    def _score_from_cache(data: dict, name: str = ""):
        """缓存 json / melody_extract 输出 → sing_song 用的乐谱字典。"""
        patterns = [int(n) for n in (data.get("syllables") or [])]
        return {
            "name": name or data.get("name") or "扒来的歌",
            "extracted": True,
            "lines": data["lines"],
            "patterns": patterns,
            "bpm": float(data.get("bpm") or 96),
            "range": list(data.get("range") or [0, 0]),
            "demo": "|".join("啦" * n for n in patterns),
            "mood": "开心",
        }


exit_plugin_main(globals(), _REAL_MODULE_NAME)
