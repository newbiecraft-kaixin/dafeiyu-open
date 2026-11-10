"""音乐/点歌 mixin（从 main.py 机械迁移，方法体未改动）。"""

from __future__ import annotations

import asyncio
import os
import re


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .compat import enter_plugin_main, exit_plugin_main, install_helpers
from .kugou import (
    SHARE_DIR,
    _kugou_cookie,
    _load_kugou_login,
    _save_kugou_cookie,
    _search_songs,
)

install_helpers(globals())
_REAL_MODULE_NAME = enter_plugin_main(globals())


class MusicMixin:
    @filter.llm_tool(name="search_song")
    async def search_song(self, event: AstrMessageEvent, song: str = ""):
        """查歌：只看本地乐曲资源库有没有这首。
        这个工具只查不下载。没有就直接说「没有《歌名》这首歌」，
        不要问用户要不要下载（下载原曲由主人用 /扒歌 完成）。

        Args:
            song(string): 歌名，可以带上歌手（如「稻香 周杰伦」）
        """
        name = re.sub(r"[《》\"'“”]", "", str(song or "")).strip()
        if not name:
            return "要查哪首歌？"
        lib = _load_music_lib()
        if lib is None:
            return "乐曲资源库没加载起来（music_lib.py 缺失）"
        hits = lib.library_hits(MUSIC_LIB, name)
        if hits:
            names = "、".join(
                f"《{h.get('name')}》{h.get('artist') or ''}".strip() for h in hits)
            return f"资源库里已经有 {names}，直接翻唱就行，不用下载。"
        return f"没有《{name}》这首歌。"

    async def fetch_song(self, event: AstrMessageEvent, song: str = "",
                         choice: str = "1"):
        """把一首歌下进乐曲资源库（自动标出高潮位置，翻唱时只唱/只发高潮那 45 秒）。
        只给主人的 /扒歌 命令用，模型不要主动调用；唱歌时曲库没有就直说没有这首歌。
        choice 是候选编号（多条搜索结果时选第几首），默认第 1 首。

        Args:
            song(string): 歌名
            choice(string): 候选编号（search_song 列表里的序号），默认 1
        """
        name = re.sub(r"[《》\"'“”]", "", str(song or "")).strip()
        if not name:
            return "要下哪首歌？"
        lib = _load_music_lib()
        if lib is None:
            return "乐曲资源库没加载起来（music_lib.py 缺失）"
        local = _find_music_file(name)
        stale = ""
        if local:
            stale = os.path.basename(local)
            # 旧版本没校验时长，可能把酷狗 60 秒试听当整首存下来：这种要重扒
            if not lib.is_preview_entry(MUSIC_LIB, stale):
                return f"资源库里已经有《{name}》了（{stale}），直接翻唱就行。"
            logger.info(f"[basic_tools] 曲库里的 {stale} 只是试听片段，重新扒整首")
        try:
            idx = max(1, int(str(choice or "1").strip() or 1))
        except ValueError:
            idx = 1
        try:
            songs = await asyncio.to_thread(_search_songs, lib, name)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[basic_tools] 下歌前搜索失败: {type(e).__name__}: {e}")
            return f"网上搜《{name}》失败了（{type(e).__name__}）"
        if not songs:
            return f"网上也没搜到《{name}》。"
        try:
            chosen = songs[idx - 1]
        except IndexError:
            return f"没有第 {idx} 首，只搜到 {len(songs)} 首。"
        # 改版/翻唱音源默认不下：还有原唱就换成原唱，只剩改版才将就用
        tag = f"{chosen['name']} {chosen['artist']}"
        if _is_derivative(tag):
            clean = [s for s in songs
                     if not _is_derivative(f"{s['name']} {s['artist']}")]
            if clean:
                logger.info(f"[basic_tools] {chosen['name']}-{chosen['artist']} 是非原唱，"
                            f"改下 {clean[0]['name']}-{clean[0]['artist']}")
                chosen = clean[0]
        rest = [s for s in songs if s is not chosen
                and not _is_derivative(f"{s['name']} {s['artist']}")]
        picks = [chosen] + rest[:1]
        # 不再补发「正在下载」：下载几十秒后工具就返回结果，模型自己会回一句，
        # 之前先直发一条只会让用户连着收到两条意思一样的话
        errors = []
        for s in picks:
            try:
                info = await asyncio.to_thread(
                    lib.fetch_to_library, MUSIC_DIR, MUSIC_LIB, s, 45,
                    _kugou_cookie())
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[basic_tools] 下载失败: {type(e).__name__}: {e}")
                errors.append(f"{s['name']}：{type(e).__name__}")
                continue
            if info.get("ok"):
                # 重扒成功：删掉旧的试听残片，免得两份同名文件混在曲库里
                new_name = os.path.basename(str(info.get("file") or ""))
                if stale and stale != new_name:
                    try:
                        os.remove(os.path.join(MUSIC_DIR, stale))
                    except OSError:
                        pass
                note = "（酷狗只给试听，已换咪咕音源）" if info.get("switched_source") else ""
                return (
                    f"下好了：《{info['name']}》"
                    + (f" - {info['artist']}" if info.get("artist") else "")
                    + f" 整首 {info['duration']:.0f} 秒已进资源库，"
                    f"高潮在第 {info['hook_start']:.0f} 秒起"
                    f"（翻唱只发这 {info['hook_len']:.0f} 秒），现在可以翻唱了。{note}"
                )
            errors.append(f"{s['name']}：{info.get('error') or '下载失败'}")
        return "没下下来：" + "；".join(errors[:3]) + "。换一首试试？"

    def _song_list_text(self, page: str = "1") -> str:
        """曲库分页文本：/听歌 无参、数字翻页、/歌曲 同义，共用同一份列表。"""
        lib = _load_music_lib()
        entries = lib.library_list(MUSIC_LIB) if lib is not None else []
        lines = []
        indexed = set()
        os.makedirs(MUSIC_DIR, exist_ok=True)
        for it in entries:
            f = str(it.get("file") or "")
            if not f or not os.path.exists(os.path.join(MUSIC_DIR, f)):
                continue
            indexed.add(f)
            dur = float(it.get("duration") or 0)
            start = float(it.get("hook_start") or 0)
            hook_len = int(it.get("hook_len") or 45)
            name = str(it.get("name") or "")
            artist = str(it.get("artist") or "")
            title = f"{name} - {artist}" if artist else name
            lines.append(
                f"{title}（{int(dur // 60)}:{int(dur % 60):02d}，"
                f"高潮 {int(start // 60)}:{int(start % 60):02d} 起 {hook_len} 秒）"
            )
        for fn in sorted(os.listdir(MUSIC_DIR)):
            if not fn.lower().endswith((".mp3", ".m4a", ".flac", ".wav", ".ogg")):
                continue
            if fn in indexed:
                continue
            path = os.path.join(MUSIC_DIR, fn)
            sec = lib.duration_of(path) if lib is not None else 0.0
            if not sec:
                sec = _audio_seconds(path)
            stem = os.path.splitext(fn)[0]
            lines.append(f"{stem}（{sec:.0f} 秒，本地曲库）" if sec
                         else f"{stem}（本地曲库）")
        if not lines:
            return "曲库还是空的。"
        size = 10
        total = (len(lines) + size - 1) // size
        try:
            p = max(1, min(int(str(page).strip() or 1), total))
        except ValueError:
            p = 1
        start = (p - 1) * size
        body = "\n".join(
            f"{i + 1}. {t}" for i, t in enumerate(lines[start:start + size], start)
        )
        tail = f"\n第 {p}/{total} 页"
        if p < total:
            tail += f" · 下一页 /听歌 {p + 1}"
        return f"曲库（{len(lines)} 首）：\n{body}{tail}"

    def _remove_song_text(self, keyword: str) -> str:
        """从曲库删一首：先删资源库条目，再删音频文件（含没登记的同名文件）。"""
        lib = _load_music_lib()
        if lib is None:
            return "乐曲资源库没加载起来（music_lib.py 缺失）"
        name = re.sub(r"[《》\"'“”]", "", str(keyword or "")).strip()
        if not name:
            return "要移除哪首？发 /听歌 移除 歌名。"
        removed = lib.library_remove(MUSIC_LIB, name)
        gone, failed = [], []
        for it in removed:
            f = str(it.get("file") or "")
            label = f"《{it.get('name') or name}》" + (
                f" - {it['artist']}" if it.get("artist") else "")
            if f:
                try:
                    os.remove(os.path.join(MUSIC_DIR, f))
                except FileNotFoundError:
                    pass
                except OSError as e:  # noqa: BLE001
                    failed.append(f"{label}（文件删不掉：{e.strerror or e}）")
                    continue
            gone.append(label)
        # 资源库里没登记、但目录里躺着的音频也一起清掉
        indexed = {str(it.get("file") or "") for it in lib.library_list(MUSIC_LIB)}
        if os.path.isdir(MUSIC_DIR):
            for fn in sorted(os.listdir(MUSIC_DIR)):
                if not fn.lower().endswith((".mp3", ".m4a", ".flac", ".wav", ".ogg")):
                    continue
                if fn in indexed or name.lower() not in fn.lower():
                    continue
                try:
                    os.remove(os.path.join(MUSIC_DIR, fn))
                    gone.append(os.path.splitext(fn)[0])
                except OSError as e:  # noqa: BLE001
                    failed.append(f"{fn}（文件删不掉：{e.strerror or e}）")
        if not gone and not failed:
            return f"曲库里没有《{name}》，没删。"
        parts = []
        if gone:
            parts.append("移除了：" + "、".join(gone))
        if failed:
            parts.append("没删掉：" + "；".join(failed))
        return "；".join(parts) + "。"

    async def _kugou_qr_flow(self, event: AstrMessageEvent):
        """酷狗没登录时私聊发二维码，扫码成功把 Cookie 写盘。"""
        mod = _load_kugou_login()
        if mod is None:
            await _hint_private(event, "酷狗扫码模块没加载起来。")
            return
        qr = await mod.start_qr()
        if not qr:
            await _hint_private(event, "酷狗二维码没拿到。")
            return
        png, sid = qr
        os.makedirs(SHARE_DIR, exist_ok=True)
        path = os.path.join(SHARE_DIR, f"kugou_qr_{sid}.png")
        with open(path, "wb") as f:
            f.write(png)
        url = f"http://127.0.0.1:8712/_share/{os.path.basename(path)}"
        await _hint_private(event, f"扫码登录酷狗（二维码已私发，别让群里的人扫）：{url}",
                            image_url=url)
        cookie = await mod.wait_login(sid, timeout=150)
        try:
            os.remove(path)
        except OSError:
            pass
        if not cookie:
            await _hint_private(event, "扫码超时。")
            return
        _save_kugou_cookie(cookie)
        await _hint_private(event, "酷狗登录好了。")

    @filter.command("听歌", alias={"歌曲"})
    async def cmd_listen_song(self, event: AstrMessageEvent, song: str = ""):
        """/听歌 [歌名或页码] —— 不带参数列曲库，数字翻页，歌名直接翻唱；
        /听歌 完整 歌名 —— 直接发曲库里的整首原曲；
        /听歌 移除 歌名 —— 从曲库删掉这首歌（仅主人）"""
        if str(event.get_platform_name() or "").lower() in (
                "wechatauto", "weixin_oc"):
            await _hint_private(event, "微信端只能发文字，唱不了歌；/听歌 只在 QQ 用。")
            return
        name = (song or "").strip()
        # AstrBot 按空格切参数，song 只拿得到第一个词；完整歌名从原始消息里取
        raw = re.sub(r"\s+", " ", str(event.get_message_str() or "")).strip()
        pieces = raw.split(" ", 1)
        if len(pieces) > 1 and pieces[1].strip():
            name = pieces[1].strip()
        if not name:
            await _hint_private(
                event,
                self._song_list_text("1")
                + "\n\n发 /听歌 歌名 唱高潮段，发 /听歌 完整 歌名 翻唱整首，"
                + "发 /听歌 原曲 歌名 听原曲文件，发 /听歌 移除 歌名 删掉。")
            return
        m = re.match(r"^(?:移除|删除|删掉|删)\s*(.*)$", name)
        if m:
            if not self._is_admin(event):
                await _hint_private(event, "只有主人能删曲库里的歌。")
                return
            await _hint_private(event, self._remove_song_text(m.group(1)))
            return
        # 原曲文件：/听歌 原曲 歌名（也认 原唱 / 原声），直接发曲库里的整首原曲
        m = re.match(r"^(?:原曲|原唱|原声)\s*(.*)$", name)
        if m:
            key = re.sub(r"[《》\"'“”]", "", m.group(1 or "")).strip()
            if not key:
                await _hint_private(event, "要听哪首的原曲？发 /听歌 原曲 歌名。")
                return
            src = _find_music_file(key)
            if not src:
                await _hint_private(
                    event,
                    f"曲库里没有《{key}》的完整原曲，先用 /扒歌 {key} 下进库。")
                return
            file_url = "file:///" + src.replace("\\", "/")
            fname = os.path.basename(src)
            try:
                await self._push_segments(
                    event,
                    [{"type": "file",
                      "data": {"file": file_url, "name": fname}}],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[basic_tools] 完整原曲发送失败: {type(e).__name__}: {e}")
                await _hint_private(
                    event, f"《{key}》找到了，但发不出去（{str(e)[:60]}）")
                return
            await event.send(event.plain_result(f"整首《{key}》来啦：{fname}"))
            return
        # 完整翻唱：/听歌 完整 歌名（也认 整首 / 整曲 / 完整版），唱整首而不是高潮段
        m = re.match(r"^(?:完整版?|整首|整曲|全曲)\s*(.*)$", name)
        if m:
            key = re.sub(r"[《》\"'“”]", "", m.group(1 or "")).strip()
            if not key:
                await _hint_private(event, "要翻唱哪首的完整版？发 /听歌 完整 歌名。")
                return
            name = key  # 落到下面统一走翻唱流程
        if name.isdigit():
            await _hint_private(event, self._song_list_text(name))
            return
        ret = await self.sing_song(event, song=name, cover=True)
        # 括号开头 / sing_song function 开头的是给模型看的内部说明，任何情况下
        # 都不能当消息发出去（否则用户会看到「上一首还在翻唱…」或提示原文反复出现）
        if isinstance(ret, str) and ret and not ret.lstrip().startswith(
                ("sing_song function ", "（", "(")):
            await _hint_private(event, ret)

    @filter.command("扒歌")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_fetch_song(
        self, event: AstrMessageEvent, song: str = "", choice: str = "1"
    ):
        """/扒歌 歌名 [序号] —— 网上扒一首进曲库（仅主人）"""
        # AstrBot 按空格切参数，song 只拿得到第一个词；歌名带空格时从原始消息里取
        raw_msg = re.sub(r"\s+", " ", str(event.get_message_str() or "")).strip()
        pieces = raw_msg.split(" ", 1)
        arg = pieces[1].strip() if len(pieces) > 1 else ""
        raw = re.sub(r"[《》\"'“”]", "", arg)
        m = re.match(r"(.+)\s+(\d+)$", raw)
        if m:
            name, idx = m.group(1).strip(), m.group(2)
        else:
            name, idx = raw, "1"
        if not name:
            await _hint_private(event, "要扒哪首？")
            return
        if not _kugou_cookie():
            # 没装 playwright 就跳过扫码，直接走在线搜索（酷狗搜不到会自动退回咪咕）
            if _load_kugou_login() is None:
                await _hint_private(
                    event, "酷狗扫码用不了（没装 playwright），这次直接用咪咕音源扒。")
            else:
                await self._kugou_qr_flow(event)
                if not _kugou_cookie():
                    return
        ret = await self.fetch_song(event, song=name, choice=idx)
        if isinstance(ret, str) and ret:
            await _hint_private(event, ret)


exit_plugin_main(globals(), _REAL_MODULE_NAME)
