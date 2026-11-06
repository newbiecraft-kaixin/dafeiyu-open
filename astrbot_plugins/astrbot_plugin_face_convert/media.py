"""媒体下载/落盘与视频解析：语音消息、视频字段解析、ffmpeg 抽帧与转写编排。"""

import asyncio
import math
import os
import re
import tempfile
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .utils import download_capped, file_uri_to_path, run_bounded
from .video_analysis import VideoAnalysis, frame_times, timestamp


class MediaMixin:
    async def _transcribe_voice(self, event: AstrMessageEvent, rec):
        """语音消息 -> (文字, 时长秒数)：NapCat 转 mp3 -> 本地 faster-whisper，云端兜底。"""
        try:
            file_ref = getattr(rec, "file", None) or getattr(rec, "url", None) or getattr(rec, "path", None)
            if not file_ref:
                return None, 0.0
            # 1) 让 NapCat 把语音转成 mp3（它自带 silk 解码）
            try:
                resp = await event.bot.get_record(file=file_ref, out_format="mp3")
            except Exception:
                resp = await event.bot.get_record(file=file_ref)
            mp3 = None
            if isinstance(resp, dict):
                mp3 = (resp.get("data") or {}).get("file") or resp.get("file")
            if not mp3:
                return None, 0.0
            # 2) 解析到本地文件
            path = file_uri_to_path(mp3)
            tmp_owned = False  # 自己下载的临时文件，用完负责清理
            if path.startswith("http"):
                async with aiohttp.ClientSession() as sess:
                    body = await download_capped(
                        sess,
                        path,
                        max_bytes=int(self.cfg.get("max_media_mb", 20)) * 1024 * 1024,
                        timeout_total=30,
                    )
                if not body:
                    return None, 0.0
                tmp = os.path.join(
                    tempfile.gettempdir(), f"asr_{int(time.time() * 1000)}.mp3"
                )
                with open(tmp, "wb") as f:
                    f.write(body)
                tmp_owned = True
            else:
                tmp = path
            # 3) 先探时长：太长的语音不听，省下转写算力
            dur = 0.0
            ff = self._ffmpeg_exe()
            if ff:
                try:
                    dur, _ = await self._probe_video(ff, tmp)
                except Exception:  # noqa: BLE001
                    dur = 0.0
            vlimit = int(self.cfg.get("voice_max_duration_s", 0) or 0)
            if vlimit and dur and dur > vlimit:
                return None, dur
            # 4) 转写
            try:
                return await self._asr_file(tmp), dur
            finally:
                if tmp_owned:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"[face_convert] 语音转写失败: {str(e)[:150]}")
            return None, 0.0

    @staticmethod
    def _ffmpeg_exe() -> str | None:
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            import shutil

            return shutil.which("ffmpeg")

    async def _download_ok(self, url: str, tmpdir: str) -> str | None:
        """下载并做防伪检查（避免下到报错网页）。成功返回本地路径。"""
        vf = os.path.join(tmpdir, "in.mp4")
        complete = False
        try:
            limit = self.cfg["max_media_mb"] * 1024 * 1024
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200 or (resp.content_length or 0) > limit:
                        return None
                    size = 0
                    with open(vf, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > limit:
                                return None
                            f.write(chunk)
            with open(vf, "rb") as f:
                head = f.read(12)
            if size < 1000 or head.lstrip().startswith(b"<"):
                return None
            complete = True
            return vf
        except Exception as e:
            logger.info(f"[face_convert] 下载失败: {str(e)[:80]}")
            return None
        finally:
            if not complete:
                try:
                    os.remove(vf)
                except OSError:
                    pass

    async def _resolve_video(self, event: AstrMessageEvent, comp, tmpdir: str) -> str | None:
        """把视频字段解析成本地文件（多候选兜底：url -> 路径 -> 凭单号找 NapCat 要）。"""
        file_f = getattr(comp, "file", None)
        url_f = getattr(comp, "url", None)
        path_f = getattr(comp, "path", None)
        logger.info(
            f"[face_convert] 视频字段: file={str(file_f)[:70]!r} "
            f"url={str(url_f)[:70]!r} path={str(path_f)[:70]!r}"
        )
        cands = []
        for f in (url_f, path_f, file_f):
            if f and str(f).strip() and str(f).strip() not in cands:
                cands.append(str(f).strip())
        for cand in cands:
            p = file_uri_to_path(cand)
            if p.startswith("http"):
                vf = await self._download_ok(p, tmpdir)
                if vf:
                    return vf
            elif os.path.exists(p):
                return p
            else:
                # 光杆文件名 -> 凭单号找 NapCat 提货
                try:
                    resp = await event.bot.get_file(file=cand)
                    d = resp.get("data") if isinstance(resp, dict) else None
                    rp = None
                    if isinstance(d, dict):
                        rp = d.get("file") or d.get("path") or d.get("url")
                    if not rp and isinstance(resp, dict):
                        rp = resp.get("file") or resp.get("url")
                    if rp:
                        rp = file_uri_to_path(rp)
                        logger.info(f"[face_convert] NapCat 提货: {rp[:90]}")
                        if rp.startswith("http"):
                            vf = await self._download_ok(rp, tmpdir)
                            if vf:
                                return vf
                        elif os.path.exists(rp):
                            return rp
                except Exception as e:
                    logger.info(f"[face_convert] get_file({cand[:40]}) 失败: {str(e)[:80]}")
        return None

    async def _run_ffmpeg(self, ff: str, *args: str, timeout: float = 60):
        proc = await asyncio.create_subprocess_exec(
            ff, "-hide_banner", "-nostdin", *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        return await run_bounded(proc, timeout)

    async def _probe_video(self, ff: str, video: str) -> tuple[float, bool]:
        # imageio-ffmpeg 只附带 ffmpeg，读头信息避免新增 ffprobe 部署要求。
        _, err = await self._run_ffmpeg(ff, "-i", video, timeout=15)
        info = err.decode("utf-8", "ignore")
        match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
        if not match:
            raise ValueError("无法读取视频时长")
        hours, minutes, seconds = map(float, match.groups())
        duration = hours * 3600 + minutes * 60 + seconds
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("视频时长无效")
        return duration, bool(re.search(r"Stream #[^\r\n]*Audio:", info))

    async def _analyze_video(self, event, comp, tmpdir: str, result: VideoAnalysis):
        video = await self._resolve_video(event, comp, tmpdir)
        if not video:
            result.notes.append("下载失败或文件超过大小限制")
            return
        # NapCat 共享的本地文件也执行大小检查，但只清理本插件创建的文件。
        if os.path.getsize(video) > self.cfg["max_media_mb"] * 1024 * 1024:
            result.notes.append("文件超过大小限制")
            return
        ff = self._ffmpeg_exe()
        if not ff:
            result.notes.append("服务器缺少 ffmpeg")
            return
        result.duration, has_audio = await self._probe_video(ff, video)
        skip_over = int(self.cfg.get("video_skip_over_s", 0) or 0)
        if skip_over and result.duration > skip_over:
            # 太长的视频直接不看：不抽帧、不转写，省算力也省时间
            logger.info(
                f"[face_convert] 视频 {result.duration:.0f}s 超过 {skip_over}s，没看"
            )
            return
        result.covered = min(result.duration, self.cfg["video_max_duration_s"])
        if result.duration > result.covered:
            result.notes.append(f"只分析前 {timestamp(result.covered)}，后续内容未处理")
        if not has_audio:
            result.notes.append("视频没有音轨")

        times = frame_times(result.covered, self.cfg["video_max_frames"])
        # 帧按四张一批；声音按段，队列按视频时间交错，避免只处理画面或声音。
        jobs = [(batch[0], "画面", batch) for i in range(0, len(times), 4)
                if (batch := times[i:i + 4])]
        if has_audio:
            step = self.cfg["video_audio_chunk_s"]
            jobs.extend((float(start), "声音", [min(step, result.covered - start)])
                        for start in range(0, math.ceil(result.covered), step))
        queue = asyncio.Queue()
        for job in sorted(jobs):
            queue.put_nowait(job)

        async def worker():
            while not queue.empty():
                start, kind, payload = queue.get_nowait()
                label = f"{timestamp(start)} {kind}"
                try:
                    if kind == "画面":
                        frames = []
                        for at in payload:
                            path = os.path.join(tmpdir, f"f_{at:.3f}.jpg")
                            rc, _ = await self._run_ffmpeg(
                                ff, "-loglevel", "error", "-y", "-ss", str(at),
                                "-threads", "1", "-i", video, "-map", "0:v:0",
                                "-frames:v", "1", "-an", "-filter_threads", "1",
                                "-vf", "scale='min(960,iw)':'min(960,ih)':force_original_aspect_ratio=decrease",
                                "-q:v", "4", "-threads", "1", path,
                            )
                            if rc == 0 and os.path.exists(path) and os.path.getsize(path):
                                frames.append((at, path))
                            else:
                                result.notes.append(f"{timestamp(at)} 抽帧失败")
                        label = f"{timestamp(start)}–{timestamp(payload[-1])} 画面"
                        text = await self._vl_describe(frames) if frames else None
                    else:
                        length = payload[0]
                        label = f"{timestamp(start)}–{timestamp(start + length)} 声音"
                        path = os.path.join(tmpdir, f"a_{start:.3f}.wav")
                        rc, _ = await self._run_ffmpeg(
                            ff, "-loglevel", "error", "-y", "-ss", str(start),
                            "-threads", "1", "-i", video, "-map", "0:a:0", "-vn",
                            "-ac", "1", "-ar", "16000", "-t", str(length),
                            "-c:a", "pcm_s16le", path,
                        )
                        text = (await self._asr_file(path)
                                if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 44
                                else None)
                    if text:
                        result.items.append((start, label, text))
                    else:
                        result.notes.append(f"{label}没有识别到内容")
                except Exception as e:
                    logger.warning(f"[face_convert] {label}失败: {type(e).__name__}")
                    result.notes.append(f"{label}识别失败，其他结果保留")
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self.cfg["video_workers"])]
        try:
            await asyncio.gather(*workers)
        finally:
            # 收回所有任务之后才能删除临时文件，不能让 ffmpeg / HTTP 留在后台。
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _process_video(self, event: AstrMessageEvent, comp) -> VideoAnalysis:
        """最长五分钟，超时/单段失败仍返回已识别结果；外部取消继续传播。
        全本地链路（faster-whisper 字幕 + Ollama 视觉）可用时不要求云端 key。"""
        result = VideoAnalysis()
        with tempfile.TemporaryDirectory(prefix="vid_") as tmpdir:
            try:
                await asyncio.wait_for(
                    self._analyze_video(event, comp, tmpdir, result),
                    timeout=self.cfg["video_timeout_s"],
                )
            except asyncio.TimeoutError:
                result.notes.append("处理超时，未完成部分已停止，已识别内容保留")
            except Exception as e:
                logger.warning(f"[face_convert] 视频处理失败: {type(e).__name__}")
                result.notes.append("视频解析失败，已识别内容保留")
        logger.info(f"[face_convert] 视频解读: {len(result.items)}段结果，{len(result.notes)}条提示")
        return result
