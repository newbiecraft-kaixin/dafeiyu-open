"""窗口操作：让路、前台切换、可见性，以及 wechatauto 默认行为的收敛。"""

from __future__ import annotations

import ctypes
import os
import time

from . import state
from .config import (
    CLICK_MODE,
    DESKTOP_GUARD,
    DND_GUARD,
    DND_PROCS,
    GUARD_IDLE_SECONDS,
    GUARD_MAX_WAIT,
)
from .util import log


class WindowBusy(RuntimeError):
    """前台是勿扰应用（模拟器等），这次不抢窗口。"""


def gui():
    return getattr(state.wx, "_gui", None) if state.wx is not None else None


def idle_seconds() -> float:
    """距用户最后一次键鼠输入过了多少秒（GetLastInputInfo，全局）。"""
    try:
        class _LII(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = _LII()
        lii.cbSize = ctypes.sizeof(_LII)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return (ctypes.windll.kernel32.GetTickCount64() - int(lii.dwTime)) / 1000.0
    except Exception:  # noqa: BLE001
        return 0.0


def foreground_hwnd() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return 0


def wait_user_idle() -> None:
    """用户正在操作就等他停手，避免把输入抢走 / 把他窗口最小化。"""
    g = gui()
    main = getattr(g, "main_hwnd", 0) if g else 0
    if not DESKTOP_GUARD or not main:
        return
    if foreground_hwnd() == main:
        return
    deadline = time.time() + GUARD_MAX_WAIT
    while time.time() < deadline:
        if idle_seconds() >= GUARD_IDLE_SECONDS:
            return
        time.sleep(0.25)


def _process_name(hwnd: int) -> str:
    """取窗口所属进程的可执行文件名（小写），取不到返回空串。"""
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        pid = ctypes.c_ulong(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = k.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""


def dnd_foreground() -> str:
    """前台是勿扰应用就返回进程名，否则返回空串。"""
    if not DND_GUARD or not DND_PROCS:
        return ""
    name = _process_name(foreground_hwnd())
    if not name:
        return ""
    return name if any(hint in name for hint in DND_PROCS) else ""


def wait_dnd_clear() -> None:
    """前台是模拟器等勿扰应用时，等它让出前台；等不到就放弃这次发送。

    模拟器（MuMu / 雷电 / 蓝叠…）会独占鼠标，微信一被激活就把它的输入抢走。
    这里宁可这次不发，也不把微信顶上来。
    """
    if not DESKTOP_GUARD:
        return
    name = dnd_foreground()
    if not name:
        return
    log("前台是勿扰应用，暂不抢窗口，等待让出:", name)
    deadline = time.time() + GUARD_MAX_WAIT
    while time.time() < deadline:
        time.sleep(0.5)
        if not dnd_foreground():
            return
    raise WindowBusy(
        f"前台是 {name}，这次不抢窗口（等了 {int(GUARD_MAX_WAIT)} 秒仍占用）"
    )


def heal_binding() -> None:
    """微信中途重启/更新后，缓存的窗口句柄会失效。

    失效后 GetWindowRect 返回 0×0，截图与坐标全部非法，所有发送都会报
    ``height and width must be > 0``；而收消息走的是数据库/监听链路，不受
    影响，所以表现为「能收不能发」。这里检测并重新挂到当前微信窗口。
    """
    g = gui()
    if g is None or not getattr(g, "main_hwnd", 0):
        return
    try:
        alive = bool(ctypes.windll.user32.IsWindow(g.main_hwnd))
    except Exception:  # noqa: BLE001
        alive = False
    rect_ok = getattr(g, "render_w", 0) > 60 and getattr(g, "render_h", 0) > 60
    if alive and rect_ok:
        return
    try:
        reacquire = getattr(g, "_reacquire_main_window", None)
        if callable(reacquire) and reacquire():
            log("微信窗口句柄失效，已自动重挂", f"hwnd={g.main_hwnd}")
            return
        g._update_render_rect()
    except Exception as exc:  # noqa: BLE001
        log("微信窗口自动重挂失败:", exc)


def begin_window_op() -> int:
    """进入窗口操作：等用户停手 → 记下原前台 → 把微信拉上来。

    微信已经在前台（当前就是目标窗口）时不切换、不置顶、不等待，
    直接返回，少一次窗口跳动，也避免无谓地抢焦点。
    """
    heal_binding()
    prev = foreground_hwnd()
    g = gui()
    main = getattr(g, "main_hwnd", 0) if g else 0
    if main and prev == main:
        return prev
    wait_dnd_clear()
    wait_user_idle()
    ensure_visible()
    return prev


def _put_back(prev: int) -> None:
    """把微信放回用户原来那个窗口的下面（恢复 Z 序）。

    ``release_foreground`` 只取消置顶，窗口本身仍然停在最前面；发送完再把
    它插到原前台窗口之后，用户眼前的窗口就不会被微信顶走。
    """
    g = gui()
    main = getattr(g, "main_hwnd", 0) if g else 0
    if not main or not prev or prev == main:
        return
    try:
        u = ctypes.windll.user32
        if not u.IsWindow(prev):
            return
        u.SetWindowPos(
            main, prev, 0, 0, 0, 0,
            0x0002 | 0x0001 | 0x0010,  # NOMOVE | NOSIZE | NOACTIVATE
        )
    except Exception as exc:  # noqa: BLE001
        log("恢复窗口层级失败:", exc)


def end_window_op(prev: int) -> None:
    """收尾：取消置顶、还原本对象最小化过的窗口，再把焦点还给人。"""
    g = gui()
    if g is not None:
        try:
            g.release_foreground()
        except Exception as exc:  # noqa: BLE001
            log("release_foreground failed:", exc)
    _put_back(prev)
    if prev and prev != foreground_hwnd():
        try:
            if ctypes.windll.user32.IsWindow(prev):
                ctypes.windll.user32.SetForegroundWindow(prev)
        except Exception:  # noqa: BLE001
            pass


def tame_gui() -> None:
    """关掉 wechatauto 的「最小化所有遮挡窗口」行为。

    默认 ensure_visible 会把用户桌面上所有和微信重叠的窗口最小化且不还原，
    这是「抢窗口」的根因。这里改成只把微信自己拉上来，不碰别人的窗口。
    """
    g = gui()
    if g is None or getattr(g, "_dfy_tamed", False):
        return
    g._minimize_blockers = lambda: 0

    # 会话定位简化：默认禁用「OCR 找会话行 + 固定坐标点击」，
    # 让 open_chat 走搜索框控件路线，不再依赖屏幕位置。
    if CLICK_MODE != "ocr":
        g.find_session = lambda *a, **k: None
        log("会话定位：只用 UIA 控件，已关闭侧栏固定坐标点击")

    # wechatauto 每次发送都会调 gui._get_db() 做「水位读取 / 回读校验」：
    # 它靠 psutil 枚举微信主进程句柄定位账号，微信 4.x 主进程拒绝枚举（AccessDenied），
    # 于是每条消息稳定抛 2 次 RuntimeError，日志被刷屏、回读校验也失效。
    # 这里改用附着时已经解密的 DB（账号与窗口一致），失败不再抛错。
    attached_db = getattr(state.wx, "_db", None) if state.wx is not None else None
    original_get_db = getattr(g, "_get_db", None)
    if attached_db is not None and callable(original_get_db):
        cached = {"db": None, "ts": 0.0}

        def _get_db():
            now = time.time()
            if cached["db"] is not None and now - cached["ts"] < 600:
                return cached["db"]
            try:
                db = original_get_db()
            except Exception:  # noqa: BLE001  微信拒绝句柄枚举时回退附着库
                db = attached_db
            cached["db"] = db
            cached["ts"] = now
            return db

        g._get_db = _get_db

    def _ensure_visible(keep_topmost: bool = True) -> bool:
        name = dnd_foreground()
        if name:
            raise WindowBusy(f"前台是 {name}，这次不抢窗口")
        try:
            heal_binding()
            u = ctypes.windll.user32
            hwnd = getattr(g, "main_hwnd", 0)
            if hwnd and u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：还原但不抢焦点
            # 微信已经是前台就不再置顶（避免多一次窗口跳动）
            if not hwnd or foreground_hwnd() != hwnd:
                g.bring_to_front(keep_topmost=False)
            g._update_render_rect()
            g._last_visible_ok = True
            g._last_visible_ts = time.time()
            return True
        except Exception as exc:  # noqa: BLE001
            log("ensure_visible failed:", exc)
            return False

    g.ensure_visible = _ensure_visible

    # 微信本来就在前台时，wechatauto 每条消息仍会重新扫窗口并激活一次。
    # 这里给 UIA 驱动加个短路：已是目标窗口且引擎还在，直接放行。
    original_get_uia = getattr(g, "_get_uia", None)
    if callable(original_get_uia):
        patched: dict = {"driver": None}

        def _get_uia(refresh: bool = False):
            drv = original_get_uia(refresh=refresh)
            if drv is not None and patched["driver"] is not drv:
                patched["driver"] = drv
                orig_ensure = drv.ensure_window

                def ensure_window(wake: bool = True, timeout=None):
                    main = getattr(g, "main_hwnd", 0)
                    if (main and foreground_hwnd() == main
                            and getattr(drv, "_win", None) is not None):
                        return True
                    return orig_ensure(wake=wake, timeout=timeout)

                drv.ensure_window = ensure_window
            return drv

        g._get_uia = _get_uia

    g._dfy_tamed = True


def ensure_visible() -> None:
    """把微信弄到可见/前台；已经在前台就什么都不做。"""
    if not state.wx:
        raise RuntimeError("微信未附着")
    name = dnd_foreground()
    if name:
        raise WindowBusy(f"前台是 {name}，这次不抢窗口")
    heal_binding()
    g = gui()
    if not g:
        return
    try:
        hwnd = getattr(g, "main_hwnd", 0)
        u = ctypes.windll.user32
        if hwnd and u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        if hwnd and foreground_hwnd() != hwnd:
            g.bring_to_front(keep_topmost=False)
    except Exception as exc:  # noqa: BLE001
        log("ensure_visible failed:", exc)
