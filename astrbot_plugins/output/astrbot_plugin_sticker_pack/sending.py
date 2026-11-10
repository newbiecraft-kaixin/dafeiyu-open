"""表情转换为消息段 / payload，以及带降级重试的出站发送。"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import tempfile
import uuid

from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Face, Image


class SendingMixin:
    @staticmethod
    def _to_component(item: dict):
        if item.get("kind") in ("face", "super"):
            return Face(id=item["value"])
        return Image.fromFileSystem(item["value"])

    def _to_payload(self, item: dict) -> dict:
        # 超级表情走的也是 face 段，id 是 QSid（≥360）：单条纯 face 在 QQ 里就是大表情
        if item.get("kind") in ("face", "super"):
            return {"type": "face", "data": {"id": str(item["value"])}}
        src = item["value"]
        # 动图走本地路径：NapCat 收到 base64:// 的 gif 时走的是「图片转码上传」通道，
        # 实测发出去只剩首帧（用户看到的就是「超级表情发了但不动」）。
        # 给本地绝对路径则直接读原文件上传，帧序列完整保留。
        is_gif = src.lower().endswith(".gif")
        if not is_gif:
            # 光看扩展名不够：pool 里有一批真·GIF 被存成了 .jpg/.png，
            # 按扩展名判断就会走 base64 通道，发出去只剩首帧（用户看到的就是静态图）。
            # 这里按文件头再认一次，是 GIF 就仍走本地路径直传。
            try:
                with PILImage.open(src) as im:
                    is_gif = im.format == "GIF" or int(getattr(im, "n_frames", 1) or 1) > 1
            except Exception:  # noqa: BLE001
                is_gif = False
        if is_gif and os.path.exists(src):
            data: dict = {"file": "file:///" + src.replace("\\", "/").lstrip("/")}
        else:
            with open(src, "rb") as f:
                data = {"file": "base64://" + base64.b64encode(f.read()).decode()}
        # sub_type=1 走 QQ 的「表情」通道（picSubType），显示成表情包/超大表情；
        # 不传的话 NapCat 按 picSubType=0 发，群里就是张普通图片，点了还会放大看图。
        sub = int(self.config.get("image_sub_type", 1))
        if sub >= 0:
            data["sub_type"] = sub
            data["summary"] = self.config.get("image_summary", "[动画表情]")
        # _src 给 _push 的降级用（压缩重发需要原文件路径），发出去前会被 pop 掉
        return {"type": "image", "data": data, "_src": src}

    def _remember_bot(self, event, umo: str) -> None:
        """缓存 bot / self_id：后台单独发表情没有 event，必须靠它做动作路由"""
        try:
            bot_obj = getattr(event, "bot", None)
            if bot_obj is not None:
                self._bots[umo] = bot_obj
                self._self_ids[umo] = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            pass

    async def _send_solo_stickers(self, umo: str, items: list[dict], delay: float) -> None:
        """文字先到，隔一两秒再单独甩表情——真人就是这么发的"""
        try:
            await asyncio.sleep(delay)
            # 一次只发一张：真人是「甩一张就走」，连发三张就成了刷屏
            for item in items[:1]:
                try:
                    ok = await self._push(umo, self._to_payload(item))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[sticker_pack] 单独发表情失败: {type(e).__name__}: {e}")
                    continue
                if ok:
                    self._mark_used(umo, item["name"])
                    logger.info(
                        f"[sticker_pack] 单独发表情「{item['name']}」"
                        f"({item.get('kind')}) -> {umo.rsplit(':', 1)[-1]}"
                    )
                else:
                    logger.warning(
                        f"[sticker_pack] 单独发表情未成功「{item['name']}」"
                        f"({item.get('kind')}) -> {umo}，bot={bool(self._bots.get(umo))}"
                    )
                await asyncio.sleep(random.uniform(0.4, 1.2))
        except asyncio.CancelledError:
            logger.warning(
                "[sticker_pack] 单发任务被取消，表情丢失："
                + "、".join(str(s.get("name")) for s in items)
            )
            raise

    async def _push(self, umo: str, seg: dict) -> bool:
        """发送，带两级降级。

        私聊里 sub_type=1（表情通道）实测全挂：retcode 1200
        「rich media transfer failed」——群里同样的图却没事。所以失败先退成普通图片；
        还传不上去就是文件太大（收藏的 2MB gif），压成单帧 jpg 再试一次。
        """
        seg = dict(seg)
        src = seg.pop("_src", "")
        # 出站节流：表情单独发也要排在文字后面，不跟主回复同一秒甩出去
        if self._gate is not None:
            await self._gate.wait(umo)
        if await self._push_once(umo, seg):
            return True
        data = seg.get("data") or {}
        if seg.get("type") != "image":
            return False
        # ① 表情通道失败 → 普通图片
        if "sub_type" in data:
            plain = {
                "type": "image",
                "data": {k: v for k, v in data.items() if k not in ("sub_type", "summary")},
            }
            if await self._push_once(umo, plain):
                logger.info(
                    f"[sticker_pack] 表情通道被拒，改普通图片发出（{umo.rsplit(':', 1)[-1]}）"
                )
                return True
        # ② 大图传不上去 → 缩小后重发（动图仍走 file://，保证还是动的）
        small = self._shrink_image(src)
        if small:
            try:
                payload = {
                    "type": "image",
                    "data": {
                        "file": "file:///" + small.replace("\\", "/").lstrip("/"),
                        "sub_type": int(self.config.get("image_sub_type", 1)),
                        "summary": self.config.get("image_summary", "[动画表情]"),
                    },
                }
                if await self._push_once(umo, payload):
                    logger.info(
                        f"[sticker_pack] 原图过大，压缩后补发成功（{umo.rsplit(':', 1)[-1]}）"
                    )
                    return True
                if await self._push_once(
                    umo,
                    {
                        "type": "image",
                        "data": {
                            "file": "base64://"
                            + base64.b64encode(open(small, "rb").read()).decode()
                        },
                    },
                ):
                    logger.info(
                        f"[sticker_pack] 压缩图改 base64 补发成功（{umo.rsplit(':', 1)[-1]}）"
                    )
                    return True
            finally:
                try:
                    os.remove(small)
                except OSError:
                    pass
        return False

    @staticmethod
    def _shrink_image(path: str) -> str:
        """大图降级：静态图压 480px jpg，动图缩帧后仍存 gif（绝不动画变静态）。

        原来一律 seek(0) + convert("RGB") + 存 jpg —— gif 直接被砍成首帧，
        用户看到的就是「明明发了动图结果不动」。
        """
        if not path or not os.path.exists(path):
            return ""
        try:
            with PILImage.open(path) as im:
                frames = int(getattr(im, "n_frames", 1) or 1)
                if frames > 1:
                    # 动图：逐帧缩到 240px，256 色，仍存 gif
                    out = os.path.join(
                        tempfile.gettempdir(), f"sp_shrink_{uuid.uuid4().hex[:8]}.gif"
                    )
                    new_frames, durations = [], []
                    for i in range(min(frames, 60)):  # 60 帧封顶，防超长动图卡死
                        im.seek(i)
                        fr = im.convert("RGBA").resize(
                            (
                                max(1, int(im.width * 240 / max(im.width, im.height))),
                                max(1, int(im.height * 240 / max(im.width, im.height))),
                            )
                        )
                        bg = PILImage.new("RGB", fr.size, (255, 255, 255))
                        bg.paste(fr, mask=fr.split()[-1])
                        new_frames.append(bg.convert("P", palette=PILImage.ADAPTIVE, colors=128))
                        durations.append(im.info.get("duration", 80))
                    new_frames[0].save(
                        out,
                        "GIF",
                        save_all=True,
                        append_images=new_frames[1:],
                        duration=durations or 80,
                        loop=0,
                        optimize=True,
                    )
                    return out
                im = im.convert("RGB")
                im.thumbnail((480, 480))
                out = os.path.join(
                    tempfile.gettempdir(), f"sp_shrink_{uuid.uuid4().hex[:8]}.jpg"
                )
                im.save(out, "JPEG", quality=82)
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_pack] 图片压缩失败: {type(e).__name__}: {e}")
            return ""

    async def _push_platform(self, umo: str, seg: dict) -> bool:
        """没有 OneBot bot 引用的平台（微信）走 AstrBot 平台通道。

        微信桥已经把 wechatauto 注册成平台，context.send_message 会落到
        io-wechat 的 /send，图片/GIF 按图片发出去——和 QQ 用的是同一套表情库。
        微信没有 QQ 黄脸段，face/super 直接跳过（整不了就不硬整）。
        """
        if seg.get("type") != "image":
            logger.info(
                f"[sticker_pack] {umo.split(':', 1)[0] or '未知平台'} 不支持 "
                f"{seg.get('type')} 段，跳过"
            )
            return False
        data = seg.get("data") or {}
        src = str(data.get("file") or "")
        temp = ""
        try:
            if src.startswith("base64://"):
                raw = base64.b64decode(src[len("base64://"):])
                if raw[:6] in (b"GIF87a", b"GIF89a"):
                    suffix = ".gif"
                elif raw[:8].startswith(b"\x89PNG"):
                    suffix = ".png"
                else:
                    suffix = ".jpg"
                temp = os.path.join(
                    tempfile.gettempdir(), f"sp_push_{uuid.uuid4().hex[:8]}{suffix}"
                )
                with open(temp, "wb") as f:
                    f.write(raw)
                src = temp
            elif src.startswith("file:///"):
                src = src[8:]          # file:///C:/x -> C:/x
            elif src.startswith("file://"):
                src = src[7:]
            if not src or not os.path.isfile(src):
                logger.warning(f"[sticker_pack] 表情文件不存在，跳过: {src}")
                return False
            await self.context.send_message(umo, MessageChain([Image.fromFileSystem(src)]))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[sticker_pack] 平台通道发表情失败 {umo}: {type(e).__name__}: {e}"
            )
            return False
        finally:
            if temp:
                try:
                    os.remove(temp)
                except OSError:
                    pass
        if self._gate is not None:
            self._gate.mark(umo)
        return True

    async def _push_once(self, umo: str, seg: dict) -> bool:
        """带 self_id 路由发一条（QQ）；微信等非 OneBot 平台走平台通道。"""
        bot = self._bots.get(umo)
        gid = umo.rsplit(":", 1)[-1]
        if bot is None or not hasattr(bot, "api") or not gid.isdigit():
            # QQ 没有 bot 引用时原来直接丢图；现在统一交给平台管理器，
            # 微信就能和 QQ 一样从同一个表情库里发图了。
            return await self._push_platform(umo, seg)
        is_group = ":GroupMessage:" in umo
        params: dict = {"message": [seg]}
        params["group_id" if is_group else "user_id"] = int(gid)
        sid = self._self_ids.get(umo, "")
        if sid.isdigit():
            params["self_id"] = int(sid)
        action = "send_group_msg" if is_group else "send_private_msg"
        try:
            resp = await bot.api.call_action(action, **params)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[sticker_pack] 表情发送异常 -> {gid}: {type(e).__name__}: {e}"
            )
            return False
        # OneBot 失败也常以 retcode!=0 返回而不抛异常，不查就等于静默丢图
        code = (resp or {}).get("retcode", 0) if isinstance(resp, dict) else 0
        if code not in (0, None):
            logger.warning(
                f"[sticker_pack] 表情发送被拒 -> {gid}: retcode={code} "
                f"{(resp or {}).get('message') or (resp or {}).get('wording') or ''}"
            )
            return False
        if self._gate is not None:
            self._gate.mark(umo)
        return True
