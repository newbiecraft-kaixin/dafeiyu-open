"""QQ 空间网络层：cookie、qzonetoken、抓取、发评、点赞。"""

from __future__ import annotations

import json
import re
import time

import aiohttp

from astrbot.api import logger

from .qzone_config import (
    H5_HOME,
    LIKE_URL,
    MOBILE_UA,
    MSG_LIST_CANDIDATES,
    RE_FEEDS_URL,
    UA,
)
from .qzone_web import (
    _extract_json_array,
    _extract_qzonetoken,
    _flatten_comments,
    _parse_frontpage,
    _parse_jsonp,
    _strip_html,
)


class QzoneClientMixin:
    async def _get_auth(self):
        """拿 qzone 域名的 cookie + g_tk(bkn)。

        NapCat 偶尔只回部分 cookie（实测出现过只有 p_skey、没有 uin 的情况），
        这种残缺 cookie 会让整块空间功能罢工。这里把几个 qzone 域名的
        cookie 合并去重；uin 还找不到就直接问客户端 get_login_info，最后才用上次记住的。
        """
        merged: dict[str, str] = {}
        bkn = ""
        # NapCat 的 cookie 缓存按域名分开存，qzone.qq.com 可能是空的而别的 qzone 域名有，
        # 所以逐个试；qq.com 放最后兜底。
        for domain in (
            "qzone.qq.com",
            "user.qzone.qq.com",
            "h5.qzone.qq.com",
            "mobile.qzone.qq.com",
            "qq.com",
        ):
            try:
                resp = await self._bot.api.call_action("get_cookies", domain=domain) or {}
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[qzone_channel] get_cookies({domain}) 失败: {type(e).__name__}: {e}")
                continue
            if not bkn:
                bkn = str(resp.get("bkn") or "")
            for part in str(resp.get("cookies") or "").split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    merged.setdefault(k.strip(), v.strip())
            # 有写接口必需的 skey/p_skey、也有 uin 才收工；缺一个就再问 qq.com 域补齐
            if (merged.get("p_skey") or merged.get("skey")) and (
                merged.get("uin") or merged.get("p_uin")
            ):
                break
        if not merged:
            raise RuntimeError("get_cookies 返回空 cookie")
        if not bkn:
            raise RuntimeError("get_cookies 没返回 bkn")
        cookies = "; ".join(f"{k}={v}" for k, v in merged.items())

        uin = ""
        for key in ("uin", "p_uin", "o_uin", "luin"):
            m = re.match(r"o?(\d{5,12})", str(merged.get(key) or ""))
            if m:
                uin = m.group(1)
                break
        if not uin:
            # cookie 里没有 uin：向客户端要登录号，别因为缺一个字段就整块功能失效
            try:
                info = await self._bot.api.call_action("get_login_info") or {}
                data = info.get("data") if isinstance(info.get("data"), dict) else info
                uin = str(data.get("user_id") or "")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[qzone_channel] get_login_info 失败: {type(e).__name__}: {e}")
        if not uin:
            uin = str(self.state.get("last_uin") or "")
        if not uin:
            raise RuntimeError(f"cookie 里没有 uin: {cookies[:120]}")
        if self.state.get("last_uin") != uin:
            self.state["last_uin"] = uin
        logger.debug(
            f"[qzone_channel] cookie 字段: {list(merged)} bkn_len={len(bkn)} uin={uin}"
        )
        return cookies, bkn, uin

    async def _get_self_nick(self) -> str:
        """拿机器人自己的 QQ 昵称，用来兜底过滤自己发的评论（缓存 1 小时）。"""
        now = time.time()
        cached = self.state.get("self_nick") or {}
        if cached.get("nick") and now - float(cached.get("ts") or 0) < 3600:
            return str(cached["nick"])
        nick = ""
        try:
            info = await self._bot.api.call_action("get_login_info") or {}
            data = info.get("data") if isinstance(info.get("data"), dict) else info
            nick = str(data.get("nickname") or "")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[qzone_channel] get_login_info 取昵称失败: {type(e).__name__}: {e}")
        if nick:
            self.state["self_nick"] = {"nick": nick, "ts": now}
            self._save_state()
        return nick

    async def _get_qzonetoken(self, sess, uin: str, cookies: str) -> str:
        """Qzone 接口要 qzonetoken（页面里的 g_qzonetoken），缺了会回 -10000「使用人数过多」。"""
        now = time.time()
        cached = self.state.get("qzonetoken") or {}
        if cached.get("uin") == uin:
            age = now - float(cached.get("ts") or 0)
            if cached.get("token") and age < 1800:
                return str(cached["token"])
            if not cached.get("token") and age < 600:
                # 拿不到 token 也记 10 分钟，别每点一次赞就把三页都重试一遍
                return ""
        pages = [
            (f"https://user.qzone.qq.com/{uin}", UA, f"https://user.qzone.qq.com/{uin}"),
            ("https://h5.qzone.qq.com/mqzone/index", MOBILE_UA, H5_HOME),
            (f"https://h5.qzone.qq.com/mqzone/profile?hostuin={uin}&refresh=0&darkMode=0",
             MOBILE_UA, H5_HOME),
            (f"https://mobile.qzone.qq.com/{uin}", MOBILE_UA, H5_HOME),
        ]
        for page, ua, ref in pages:
            headers = {"Cookie": cookies, "User-Agent": ua, "Referer": ref}
            try:
                async with sess.get(page, headers=headers) as r:
                    html = await r.text()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[qzone_channel] 取 qzonetoken {page} 失败: {e}")
                continue
            token = _extract_qzonetoken(html)
            if token:
                self.state["qzonetoken"] = {"uin": uin, "token": token, "ts": now}
                self._save_state()
                logger.info(f"[qzone_channel] 已获取 qzonetoken（{len(token)} 字，来源 {page}）")
                return token
            head = re.sub(r"\s+", " ", html[:120])
            idx = html.lower().find("qzonetoken")
            near = re.sub(r"\s+", " ", html[max(0, idx - 60):idx + 180]) if idx >= 0 else ""
            logger.debug(
                f"[qzone_channel] {page} 无 token，head={head!r}"
                + (f" 附近={near!r}" if near else "")
            )
        return ""

    async def _fetch_feeds(self, cookies: str, bkn: str, uin: str) -> list:
        """主通道：h5 空间首页把说说和评论直接内嵌在 HTML 里，不需要 qzonetoken。

        PC 的 emotion_cgi_msglist_v6 对机器人号一律回 -10000「使用人数过多」，所以先走 h5；
        h5 也拿不到时再退回老接口。
        """
        try:
            feeds = await self._fetch_feeds_h5(cookies, uin)
            if feeds:
                logger.info(f"[qzone_channel] 拉取说说 {len(feeds)} 条（h5 首页）")
                return feeds
            logger.debug("[qzone_channel] h5 首页没有自己的说说，转老接口")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[qzone_channel] h5 首页拉取失败: {type(e).__name__}: {e}")
        return await self._fetch_feeds_legacy(cookies, bkn, uin)

    async def _fetch_feeds_h5(self, cookies: str, uin: str) -> list:
        headers = {
            "Cookie": cookies,
            "User-Agent": MOBILE_UA,
            "Referer": H5_HOME,
        }
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(H5_HOME, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"h5 首页 HTTP {r.status}")
                html = await r.text()
        data = _parse_frontpage(html)
        raw = (data.get("data") or {}).get("vFeeds") or []
        out: list = []
        for f in raw[: int(self.cfg["max_feeds"])]:
            user = (f.get("userinfo") or {}).get("user") or {}
            if str(user.get("uin") or "") != str(uin):
                continue  # 只处理自己发的说说
            comm = f.get("comm") or {}
            cm = f.get("comment") or {}
            tid = str(comm.get("ugcrightkey") or f.get("id") or "")
            if not tid:
                continue
            summary = (f.get("summary") or {}).get("summary") or ""
            clist = []
            # 关键：h5 的 comment.comments 只有顶层评论，用户「回复机器人的那条回复」时
            # 会落到 comment.replys（楼中楼）里。num/cntnum 统计的是含楼中楼的条数，
            # 只扫 comments 就会永远看不到这类回复 —— 用户发消息没被回就是这个坑。
            for c in _flatten_comments(cm.get("comments") or []):
                cu = c.get("user") or {}
                clist.append({
                    "tid": str(c.get("commentid") or ""),
                    "uin": str(cu.get("uin") or ""),
                    "name": cu.get("nickname") or "",
                    "createTime": int(c.get("date") or 0),
                    "content": c.get("content") or "",
                    # 楼中楼才有：parent 是父评论作者，target 是这条回复针对的人
                    "parent": str(c.get("_parent_uin") or ""),
                    "target": str(c.get("_target_uin") or ""),
                })
            out.append({
                "tid": tid,
                "content": _strip_html(summary),
                "commentlist": clist,
            })
        return out

    async def _fetch_feeds_legacy(self, cookies: str, bkn: str, uin: str) -> list:
        headers = {
            "Cookie": cookies,
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "User-Agent": UA,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        errors: list[str] = []
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            token = await self._get_qzonetoken(sess, uin, cookies)
            form = {
                "g_tk": bkn,
                "uin": uin,
                "ftype": 0,
                "sort": 0,
                "pos": 0,
                "num": int(self.cfg["max_feeds"]),
                "replynum": 30,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "jsonp",
                "need_private_comment": 1,
                "qzonetoken": token,
            }
            for url, method in MSG_LIST_CANDIDATES:
                try:
                    async with sess.get(url, params=form, headers=headers) as r:
                        status, raw = r.status, await r.text()
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{url.split('/cgi-bin/')[1]}: {type(e).__name__}")
                    continue
                if status != 200 or not raw.strip():
                    errors.append(f"{url.split('/cgi-bin/')[1]}: HTTP {status}")
                    continue
                try:
                    data = _parse_jsonp(raw)
                except ValueError:
                    errors.append(f"{url.split('/cgi-bin/')[1]}: 非 JSON {raw[:40]}")
                    continue
                feeds = data.get("msglist") or data.get("data") or []
                if isinstance(feeds, dict):
                    feeds = feeds.get("msglist") or []
                if not isinstance(feeds, list):
                    feeds = []
                if not feeds:
                    logger.debug(
                        f"[qzone_channel] {url.split('/cgi-bin/')[1]} 返回 0 条 "
                        f"code={data.get('code')} msg={data.get('message') or data.get('msg')} "
                        f"raw={raw[:200]!r}"
                    )
                    errors.append(f"code={data.get('code')}")
                    continue
                logger.info(
                    f"[qzone_channel] 拉取说说 {len(feeds)} 条（total={data.get('total')}）"
                )
                return feeds
            # 老接口全挂时探一下空间消息中心（评论/点赞通知，不依赖 qzonetoken）
            try:
                async with sess.get(
                    "https://h5.qzone.qq.com/webapp/json/mqzone_message/getMessageList",
                    params={"g_tk": bkn, "status": 0},
                    headers=headers,
                ) as r:
                    raw2 = await r.text()
                logger.debug(
                    f"[qzone_channel] 消息中心 HTTP {r.status} raw={raw2[:300]!r}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[qzone_channel] 消息中心探测失败: {type(e).__name__}: {e}")
        raise RuntimeError("所有 msglist 端点都失败: " + " | ".join(errors))

    async def _fetch_other_feeds(
        self, cookies: str, bkn: str, my_uin: str, target: str, limit: int = 20
    ) -> list:
        """拉别人空间的动态：h5 个人空间页把说说直接内嵌在 HTML 里，不需要 qzonetoken。

        PC 的 emotion_cgi_msglist_v6 对机器人号一律回 -10000「使用人数过多」，所以走 h5。
        """
        url = (
            f"https://h5.qzone.qq.com/mqzone/profile?hostuin={target}"
            "&refresh=0&darkMode=0"
        )
        headers = {
            "Cookie": cookies,
            "User-Agent": MOBILE_UA,
            "Referer": H5_HOME,
        }
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"h5 个人空间 HTTP {r.status}")
                html = await r.text()
        out: list = []
        for f in _extract_json_array(html, "vFeeds"):
            user = (f.get("userinfo") or {}).get("user") or {}
            if str(user.get("uin") or "") != str(target):
                continue  # 页面里可能夹带别人的转发
            summary = (f.get("summary") or {}).get("summary") or ""
            comm = f.get("comm") or {}
            like = f.get("like") or {}
            raw_id = f.get("id")
            if isinstance(raw_id, dict):
                fid = str(raw_id.get("cellid") or "")
            else:
                fid = str(raw_id or "")
            like_key = str(comm.get("curlikekey") or comm.get("orglikekey") or "")
            if not like_key and fid:
                like_key = f"http://user.qzone.qq.com/{target}/mood/{fid}.1"
            out.append({
                "content": summary,
                "comments": (f.get("comment") or {}).get("comments") or [],
                "pics": f.get("pic") if isinstance(f.get("pic"), list) else [],
                "time": comm.get("abstime") or "",
                # 点赞/留言要用的字段：feed id + owner/unikey
                "tid": str(comm.get("ugcrightkey") or fid),
                "fid": fid,
                "owner": str(target),
                "unikey": like_key,
                "appid": str(comm.get("appid") or f.get("appid") or 311),
                "isliked": int(bool(like.get("isliked"))),
                "likecount": int(like.get("num") or 0),
            })
            if len(out) >= max(1, int(limit)):
                break
        logger.info(f"[qzone_channel] 拉取 {target} 的空间动态 {len(out)} 条（h5，上限 {limit}）")
        return out

    async def _post_comment(
        self,
        cookies: str,
        bkn: str,
        uin: str,
        tid: str,
        content: str,
        host_uin: str = "",
        token: str | None = None,
    ) -> bool:
        """发一条空间评论。host_uin 是说说作者的 QQ 号（给别人留言时传对方），默认自己。

        qzonetoken 是写接口的通行证：缺了会回 -10000「使用人数过多」。
        """
        self._post_blocked = False
        self._last_post_code = None
        host = str(host_uin or uin)
        data = {
            # Qzone 的 topicId 必须是「作者uin_tid」，只传 tid 会回 -10004 参数错误
            "topicId": f"{host}_{tid}",
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "ref": "feeds",
            "content": content,
            "platform": "qzone",
            "platformid": 52,
            "appid": 311,
            "src": 1,
            "hostUin": host,
            "uin": uin,
            "format": "json",
            "needPrivateComment": 0,
        }
        headers = {
            "Cookie": cookies,
            "Referer": f"https://user.qzone.qq.com/{host}",
            "User-Agent": UA,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            tok = await self._get_qzonetoken(sess, uin, cookies) if token is None else token
            if tok:
                # web 端把 qzonetoken 和 g_tk 都塞在表单里，两处都带上更稳
                data["qzonetoken"] = tok
                data["g_tk"] = bkn
            async with sess.post(
                RE_FEEDS_URL,
                params={"g_tk": bkn},
                data=data,
                headers=headers,
            ) as r:
                raw = await r.text()
                if r.status != 200:
                    logger.warning(f"[qzone_channel] 回评 HTTP {r.status}: {raw[:120]}")
                    return False
        try:
            j = _parse_jsonp(raw)
        except ValueError:
            logger.warning(f"[qzone_channel] 回评返回非 JSON: {raw[:120]}")
            return False
        code = j.get("code", j.get("ret", 0))
        self._last_post_code = code
        if code not in (0, None):
            if code in (-10000, -10052):
                # qzonetoken 缺失或失效：写接口一律被拦（读接口不受影响）
                self._post_blocked = True
                logger.warning(
                    f"[qzone_channel] 回评接口要 qzonetoken，当前 token={'有' if data.get('qzonetoken') else '无'}"
                    f"（code={code}），这条转给主人手动发"
                )
                return False
            logger.warning(
                f"[qzone_channel] 回评被拒 code={code} msg={j.get('message') or j.get('msg')}"
                f" raw={raw[:160]}"
            )
            return False
        return True

    async def _like_feed(
        self, cookies: str, bkn: str, uin: str, feed: dict, like: bool = True,
        token: str | None = None,
    ) -> tuple[bool, str]:
        """给一条说说点赞（like=False 则取消）。返回 (是否成功, 失败原因)。"""
        owner = str(feed.get("owner") or feed.get("uin") or uin)
        fid = str(feed.get("fid") or feed.get("tid") or "")
        unikey = str(feed.get("unikey") or "") or (
            f"http://user.qzone.qq.com/{owner}/mood/{fid}.1" if fid else ""
        )
        if not unikey:
            return False, "页面没给点赞 key（接口可能改版）"
        form = {
            "qzreferrer": f"https://user.qzone.qq.com/{owner}",
            "opuin": uin,
            "unikey": unikey,
            "curkey": unikey,
            "fid": fid,
            "appid": str(feed.get("appid") or 311),
            "from": "1",
            "type": "0" if like else "1",
            "abstime": str(int(time.time())),
            "fupdate": "1",
        }
        if token:
            form["qzonetoken"] = token
        headers = {
            "Cookie": cookies,
            "Referer": f"https://user.qzone.qq.com/{owner}",
            "User-Agent": UA,
        }
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                LIKE_URL, params={"g_tk": bkn}, data=form, headers=headers
            ) as r:
                raw = await r.text()
                if r.status != 200:
                    return False, f"HTTP {r.status}"
        try:
            j = _parse_jsonp(raw)
        except ValueError:
            m = re.search(r"callback\(\s*(\{.*\})\s*\)", raw, re.S)
            if not m:
                return False, f"返回非 JSON: {raw[:80]}"
            try:
                j = json.loads(m.group(1))
            except ValueError:
                return False, f"返回非 JSON: {raw[:80]}"
        code = j.get("code", j.get("ret", 0))
        if code in (0, None):
            return True, ""
        if code in (-10000, -10052):
            return False, "需要 qzonetoken，账号拿不到"
        return False, f"code={code} {j.get('message') or j.get('msg') or ''}".strip()
