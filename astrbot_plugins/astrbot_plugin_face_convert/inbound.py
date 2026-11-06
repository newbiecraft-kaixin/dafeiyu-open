"""收信翻译：表情/图片/语音/视频/分享链接 -> 文本，并做身份标记。"""

import asyncio
import re
import tempfile
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import At, Face, Image, Plain, Record, Reply, Video
try:  # QQ 链接分享卡片（部分 NapCat 版本才有）
    from astrbot.core.message.components import Json as _JsonComp
except ImportError:  # pragma: no cover
    _JsonComp = None

from .video_analysis import VideoAnalysis


class InboundMixin:
    async def _handle_inbound_faces(self, event: AstrMessageEvent):
        """把收到的 QQ 表情翻译成 [名字] 文本。

        纯表情消息的 message_str 为空，上游会当"空消息"处理导致她装死不回；
        这里把表情名拼进文本，让整条链路正常运转。
        """
        try:
            comps = event.get_messages()
        except Exception:
            return
        # 验尸记录仪：遇到不认识的组件类型就备案（排查神秘消息用）
        known = (Face, Image, Record, Video, Plain) + (
            (_JsonComp,) if _JsonComp is not None else ()
        )
        unknown = [type(c).__name__ for c in comps if not isinstance(c, known)]
        if unknown:
            logger.debug(f"[face_convert] 未知消息组件: {unknown}")
        names = []
        img_count = 0
        recs = []
        vids = []
        for c in comps:
            if isinstance(c, Face):
                names.append(f"[{self.id2name.get(c.id, f'表情{c.id}')}]")
            elif isinstance(c, Image):
                img_count += 1
            elif isinstance(c, Record):
                recs.append(c)
            elif isinstance(c, Video):
                vids.append(c)
        # 卡片（QQ 分享的 B站/抖音/文章等）统一在下面的 _collect_video_links 里处理
        # 图片：本地 OCR 提取图上文字，识别到才替换占位符（表情包文字条也能读到）
        imgs = [c for c in comps if isinstance(c, Image)]
        # 群里默认不往 message_str 里塞「[对方发来…]」这类描述（消息栏就只看原图）；
        # 只有私聊、或者 @/引用 了她的时候才写，那时她确实需要"看懂"这张图。
        directed = not event.get_group_id()
        if not directed:
            for c in comps:
                if isinstance(c, At) and str(getattr(c, "qq", "")) == str(
                    event.get_self_id()
                ):
                    directed = True
                    break
                if isinstance(c, Reply) and str(getattr(c, "sender_id", "")) == str(
                    event.get_self_id()
                ):
                    directed = True
                    break
        img_carry = ""
        for img in imgs[:3]:
            try:
                ocr_text = await self._ocr_image(event, img)
            except Exception as e:
                logger.warning(f"[face_convert] 图片 OCR 异常: {type(e).__name__}")
                ocr_text = None
            if ocr_text:
                # 图上有字就是真内容，群里也照写——她得看得懂截图/文字图才接得上话
                names.append(f"[对方发来一张图片，上面写着：{ocr_text}]")
                if not img_carry:
                    img_carry = f"图上写着：{ocr_text}"
            else:
                # 没字的图（表情包）：群里不写占位，消息栏只看原图；
                # 只有私聊 / @她 / 引用她 时才留一句，否则那条消息是空的、她直接不回
                event.set_extra("_fy_sticker_hint", True)
                if directed:
                    names.append("[对方发来一个表情包（可能想逗你/跟你斗图）]")
                if not img_carry:
                    img_carry = "一张图片/表情包，看不出画面内容"
        # 引用消息里的图片：引用是明确指向，必须看懂。
        # 之前只遍历当前消息链，Reply.chain 里的 Image 被漏掉，
        # 「引用一条文字+图片」的消息就只剩文字，模型只能瞎猜画面。
        quoted_imgs = []
        for c in comps:
            if isinstance(c, Reply):
                for rc in (getattr(c, "chain", None) or []):
                    if isinstance(rc, Image):
                        quoted_imgs.append(rc)
        for img in quoted_imgs[:3]:
            try:
                ocr_text = await self._ocr_image(event, img)
            except Exception as e:
                logger.warning(f"[face_convert] 引用图片 OCR 异常: {type(e).__name__}")
                ocr_text = None
            if ocr_text:
                names.append(f"[引用消息里的图片，上面写着：{ocr_text}]")
            else:
                # 没字就交给本地视觉看画面：引用是「看这张」的明确指令，
                # 只回「看不到」等于没解析引用。
                desc = None
                try:
                    with tempfile.TemporaryDirectory(prefix="qv_") as qtmp:
                        qpath = await self._resolve_video(event, img, qtmp)
                        if qpath:
                            desc = await self._vl_image(qpath)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[face_convert] 引用图片视觉失败: {type(e).__name__}")
                if desc:
                    names.append(f"[引用消息里的图片，画面：{desc}]")
                else:
                    names.append("[引用消息里有一张图片，画面你看不到，别编、别猜内容]")
        # 图、字常分两条发：先甩一张头像/截图，再单独问「看我的头像」。
        # 纯图片那条文本是空的，进不了对话上下文，模型只能对着后一句瞎答。
        # 私聊里给上一张图留 90 秒图注，紧跟其后的文字消息补上。
        try:
            ctx = getattr(self, "_img_ctx", None)
            if ctx is None:
                ctx = self._img_ctx = {}
            key = str(getattr(event, "unified_msg_origin", "") or "")
            sender = str(event.get_sender_id() or "")
            base_text = (event.message_str or "").strip()
            if img_carry and not base_text and not event.get_group_id():
                ctx[key] = (time.time(), sender, img_carry)
                while len(ctx) > 64:
                    ctx.pop(next(iter(ctx)))
            elif key in ctx and not imgs and not event.get_group_id():
                ts, who, desc = ctx.pop(key)
                if who == sender and time.time() - ts <= 90:
                    names.append(f"[补充：对方刚才发来的那张图片——{desc}]")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] 图片接续失败: {type(e).__name__}")
        # 语音：群里不 @ 也一律解析——她得听得到群里在说什么，接不接话另说
        # 视频：默认不解析，抽帧 + 视觉模型太费算力，价值又低
        if vids and not self.cfg.get("video_enabled", False):
            logger.info(
                f"[face_convert] 视频不解析，跳过（{event.get_group_id() or '私聊'}）"
            )
            vids = []
        if recs:
            async with self._media_sem:
                txt, dur = await self._transcribe_voice(event, recs[0])
            vlimit = int(self.cfg.get("voice_max_duration_s", 0) or 0)
            if vlimit and dur and dur > vlimit:
                names.append(f"[对方发来一条 {int(dur)} 秒的语音，太长了没听完]")
                logger.info(f"[face_convert] 语音 {dur:.0f}s 超过 {vlimit}s，没听")
            else:
                names.append(
                    f"[对方发来一条语音，你清楚地听到他说：{txt}]" if txt
                    else "[对方发来一条语音（你没听清）]"
                )
        if vids:
            analysis = VideoAnalysis()
            acquired = False
            try:
                await asyncio.wait_for(
                    self._media_sem.acquire(), timeout=self.cfg["media_queue_timeout_s"]
                )
                acquired = True
                analysis = await self._process_video(event, vids[0])
            except asyncio.TimeoutError:
                analysis.notes.append("正在处理其他媒体，请稍后再试")
            except Exception as e:
                logger.warning(f"[face_convert] 视频处理异常: {type(e).__name__}")
                analysis.notes.append("视频处理异常")
            finally:
                if acquired:
                    self._media_sem.release()
            skip_over = int(self.cfg.get("video_skip_over_s", 0) or 0)
            if skip_over and analysis.duration > skip_over and not analysis.items:
                event.set_extra("_fy_video_incomplete", True)
                names.append(
                    f"[对方发来一段 {int(analysis.duration)} 秒的视频，太长了没看]"
                )
            else:
                event.set_extra("_fy_video_incomplete", bool(analysis.notes or not analysis.items))
                names.append(f"[对方发来的视频，识别结果：{analysis.render()}]")
        # 视频链接（裸 URL / QQ 分享卡片）：抓标题告诉 AI 对方分享了啥
        try:
            link_notes = await self._collect_video_links(event, comps)
            names.extend(link_notes)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[face_convert] 链接识别异常: {type(e).__name__}")
        if names:
            extra = " ".join(names)
            if extra not in (event.message_str or ""):
                event.message_str = ((event.message_str or "") + " " + extra).strip()
                logger.info(f"[face_convert] 收信翻译: {extra}")

        # 身份标记（只进她的上下文，对方看不到字面量）：
        # 管理员（群聊/私聊）-> [主人(QQ号)]；群聊其他人 -> [群友 昵称(QQ号)]；
        # 陌生人私聊 -> [非主人 昵称(QQ号)]。只有「主人」这三个字带身份，
        # 其余刻意不写「主人」，防止模型把随便一个群友认成主人。
        try:
            if not event.get_extra("_fy_tagged"):
                sid = str(event.get_sender_id())
                is_group = bool(event.get_group_id())
                # 群聊 / 私聊都标：私聊主人也是主人，不能因为没标记就被当成群友
                if is_group or event.is_private_chat():
                    name = re.sub(r"[\[\]\r\n]", "", (event.get_sender_name() or "").strip())[:16]
                    # 昵称里带「主人」字样（真有人叫「肥鱼主人」）会把模型带偏，剔掉再标记
                    if sid not in self.admins:
                        name = re.sub(r"主人", "", name).strip()
                    if sid in self.admins:
                        tag = f"主人({sid})"
                    elif is_group:
                        tag = f"群友 {name}({sid})" if name else f"群友({sid})"
                    else:
                        tag = f"非主人 {name}({sid})" if name else f"非主人({sid})"
                    text = (event.message_str or "").strip()
                    if text:
                        event.message_str = f"[{tag}] {text}"
                        event.set_extra("_fy_tagged", True)
                        logger.info(f"[face_convert] 身份标记: {tag}")
                    elif is_group and event.is_wake and not any(
                        type(c).__name__ == "Poke" for c in comps
                    ) and not any(
                        (isinstance(c, At) and str(c.qq) == str(event.get_self_id()))
                        or (
                            isinstance(c, Reply)
                            and str(getattr(c, "sender_id", "")) == str(event.get_self_id())
                        )
                        for c in comps
                    ):
                        # 只喊了名字、没带内容：给她一个回应的由头。
                        # 拍一戳除外：那是社交动作，由 human_sim 本地回，
                        # 补一句「（喊了你一声）」会让模型当成普通消息接话，
                        # 群里就多出一条跟拍一戳对不上的回复。
                        event.message_str = f"[{tag}] （喊了你一声）"
                        event.set_extra("_fy_tagged", True)
                        logger.info(f"[face_convert] 群聊空唤醒标记: {tag}")
        except Exception as e:
            logger.warning(f"[face_convert] 身份标记失败: {str(e)[:100]}")
