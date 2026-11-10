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

# 身份前缀缓存：昵称基本不会改，同一个人在同一个群里的
# 「[群友 昵称(QQ号)]」前缀直接复用，不用每条消息都重新取昵称再拼一遍。
# key=(群号/"private", QQ号)，value=(写入时间, 前缀)
_TAG_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
_TAG_CACHE_TTL = 12 * 3600
_TAG_CACHE_MAX = 800


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
        img_carries: list[str] = []
        for img in imgs[:3]:
            try:
                ocr_text = await self._ocr_image(event, img)
            except Exception as e:
                logger.warning(f"[face_convert] 图片 OCR 异常: {type(e).__name__}")
                ocr_text = None
            if ocr_text:
                # 图上有字就是真内容，群里也照写——她得看得懂截图/文字图才接得上话
                names.append(f"[对方发来一张图片，上面写着：{ocr_text}]")
                img_carries.append(f"图上写着：{ocr_text}")
            else:
                # 没字的图：以前在群里直接当纯媒体丢掉，接话逻辑连画面都不知道。
                # 这里补一次本地视觉，把画面写进消息串，接不接话仍由 human_sim 的概率/冷却决定。
                event.set_extra("_fy_sticker_hint", True)
                desc = None
                try:
                    with tempfile.TemporaryDirectory(prefix="gv_") as gtmp:
                        gpath = await self._resolve_video(event, img, gtmp)
                        if gpath:
                            # 动图（gif/webp）在 _vl_image 里抽帧转 png 再送，别只留首帧
                            desc = await self._vl_image(gpath, timeout=20)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[face_convert] 群图视觉失败: {type(e).__name__}")
                if desc:
                    names.append(f"[对方发来一张图片，画面：{desc}]")
                    img_carries.append(desc)
                else:
                    # 视觉也没认出来：仍旧按表情包处理，只留斗图线索
                    if directed:
                        names.append("[对方发来一个表情包（可能想逗你/跟你斗图）]")
                    if not img_carries:
                        img_carries.append("一张图片/表情包，看不出画面内容")
                    names.append("[识图失败，请在 中枢→模型→识图模型 设置识图模型]")
        img_carry = "；".join(img_carries)
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
                    names.append(
                        "[引用消息里有一张图片，画面你看不到，别编、别猜内容]"
                        "[识图失败，请在 中枢→模型→识图模型 设置识图模型]"
                    )
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
                # 连续发好几张/好几个动图时累加，别让最后一张把前面的图注顶掉
                prev = ctx.get(key)
                if prev and prev[1] == sender and time.time() - prev[0] <= 90:
                    img_carry = f"{prev[2]}；{img_carry}"
                ctx[key] = (time.time(), sender, img_carry[:300])
                while len(ctx) > 64:
                    ctx.pop(next(iter(ctx)))
            elif key in ctx and not imgs and not event.get_group_id():
                ts, who, desc = ctx.pop(key)
                if who == sender and time.time() - ts <= 90:
                    names.append(f"[补充：对方刚才发来的图片——{desc}]")
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
                # 回填群滑动窗口：纯图片消息会被 human_sim 丢弃、文字消息的
                # 上下文里也看不到图，把翻译结果写进窗口，后续任何回话都能看到
                try:
                    gid = event.get_group_id()
                    if gid:
                        # get_registered_star 返回 StarMetadata，实例在 star_cls
                        info = None
                        try:
                            info = self.context.get_registered_star(
                                "astrbot_plugin_group_window"
                            )
                        except Exception:
                            info = None
                        if info is None:
                            try:
                                from astrbot.core.star.star import star_map

                                info = star_map.get("astrbot_plugin_group_window.main")
                            except Exception:
                                info = None
                        gw = getattr(info, "star_cls", None) or getattr(info, "star", None)
                        if gw is not None and hasattr(gw, "capture_external"):
                            gw.capture_external(
                                gid,
                                event.get_sender_id(),
                                event.get_sender_name(),
                                extra,
                            )
                            logger.info("[face_convert] 图注已回填群窗口")
                except Exception as e:
                    logger.debug(f"[face_convert] 群窗口回填失败: {type(e).__name__}: {e}")

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
                    self._refresh_admins()  # 管理员名单热更新
                    # 昵称一般不改：同群同一人的前缀直接复用，命中就不再取昵称
                    ck = (
                        (str(event.get_group_id() or ""), sid)
                        if is_group
                        else ("private", sid)
                    )
                    now_ts = time.time()
                    hit = _TAG_CACHE.get(ck)
                    if hit and now_ts - hit[0] < _TAG_CACHE_TTL:
                        tag = hit[1]
                        # 管理员名单刚热更新过：缓存标签的身份段（主人/非主人）和
                        # 现名单对不上就重算，不然新管理员要背 12 小时旧标签
                        if tag.startswith("主人") != (sid in self.admins):
                            hit = None
                    if hit is None:
                        name = re.sub(
                            r"[\[\]\r\n]", "", (event.get_sender_name() or "").strip()
                        )[:16]
                        # 昵称里带「主人」字样（真有人叫「肥鱼主人」）会把模型带偏，剔掉再标记
                        if sid not in self.admins:
                            name = re.sub(r"主人", "", name).strip()
                        if sid in self.admins:
                            # 控制台给管理员配的身份标签（爸爸/母亲大人…）直接进标记，
                            # 模型按这个称呼叫TA；没配的保持「主人(QQ号)」
                            ident = str(self.admin_identities.get(sid) or "").strip()
                            if ident and ident != "主人":
                                tag = f"主人 {ident}({sid})"
                            else:
                                tag = f"主人({sid})"
                        elif is_group:
                            tag = f"群友 {name}({sid})" if name else f"群友({sid})"
                        else:
                            tag = f"非主人 {name}({sid})" if name else f"非主人({sid})"
                        if len(_TAG_CACHE) >= _TAG_CACHE_MAX:
                            _TAG_CACHE.clear()
                        _TAG_CACHE[ck] = (now_ts, tag)
                    text = (event.message_str or "").strip()
                    if text:
                        event.message_str = f"[{tag}] {text}"
                        event.set_extra("_fy_tagged", True)
                        logger.info(f"[face_convert] 身份标记: {tag}")
                    elif any(type(c).__name__ in ("Forward", "Nodes") for c in comps):
                        # 合并转发的 message_str 是空的，不给占位的话私聊/群里都像发了个空消息，
                        # 下游直接丢掉；先补一句，正文由 human_sim 的转发展开注入。
                        event.message_str = f"[{tag}] [转发了聊天记录]"
                        event.set_extra("_fy_tagged", True)
                        logger.info(f"[face_convert] 转发占位标记: {tag}")
                    elif is_group and event.is_at_or_wake_command and not any(
                        type(c).__name__ == "Poke" for c in comps
                    ) and not any(
                        (isinstance(c, At) and str(c.qq) == str(event.get_self_id()))
                        or (
                            isinstance(c, Reply)
                            and str(getattr(c, "sender_id", "")) == str(event.get_self_id())
                        )
                        for c in comps
                    ):
                        # 用 is_at_or_wake_command 而不是 is_wake：后者只要有插件 handler
                        # 匹配到消息就会被置 True（群里每条消息都是），那样别人 @ 谁、
                        # 或只发个空消息都会被写成「喊了你一声」，让它以为被 @。
                        # 只喊了名字、没带内容：给她一个回应的由头。
                        # 拍一戳除外：那是社交动作，由 human_sim 本地回，
                        # 补一句「（喊了你一声）」会让模型当成普通消息接话，
                        # 群里就多出一条跟拍一戳对不上的回复。
                        event.message_str = f"[{tag}] （喊了你一声）"
                        event.set_extra("_fy_tagged", True)
                        logger.info(f"[face_convert] 群聊空唤醒标记: {tag}")
        except Exception as e:
            logger.warning(f"[face_convert] 身份标记失败: {str(e)[:100]}")
