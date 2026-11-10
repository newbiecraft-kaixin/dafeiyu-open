# -*- coding: utf-8 -*-
"""从 DeepSeek 开放平台充值页抓取「微信支付」二维码。

用法：
    python ds_qr.py --amount 10 [--out 保存路径] [--json]
    （不指定 --out 时二维码落系统临时目录；充值页 serve.py 会现场生成并弹出）

原理：连本机 Edge 的 CDP 调试端口（9222），在已登录的 top_up 页面里
选金额 → 选微信支付 → 点「去支付」→ 把弹窗里的 canvas 导出成 PNG。
浏览器没起来时会用 WMI 拉起一个 headless 实例（复用已同步登录态的 profile）。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

CDP = "http://127.0.0.1:9222"
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "edge_profile")
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
TOPUP = "https://platform.deepseek.com/top_up"
TIERS = (10, 20, 50, 100, 300, 500)


def _get(path, timeout=5):
    return urllib.request.urlopen(CDP + path, timeout=timeout).read()


def cdp_alive():
    try:
        return b"Browser" in _get("/json/version")
    except Exception:  # noqa: BLE001
        return False


def launch_headless():
    edge = next((p for p in EDGE_CANDIDATES if os.path.exists(p)), None)
    if not edge:
        raise RuntimeError("找不到 msedge.exe")
    if not os.path.isdir(PROFILE):
        raise RuntimeError(f"登录态目录不存在：{PROFILE}（先跑 sync_profile.py）")
    cmdline = (
        f'"{edge}" --headless=new --user-data-dir="{PROFILE}" '
        "--remote-debugging-port=9222 --no-first-run --no-default-browser-check "
        "--disable-gpu about:blank"
    )
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{ CommandLine = '{cmdline}' }}; $r.ProcessId"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=30)
    for _ in range(40):
        time.sleep(0.5)
        if cdp_alive():
            return
    raise RuntimeError("Edge 调试端口未就绪")


def tabs():
    return json.loads(_get("/json/list"))


def open_tab(url):
    for t in tabs():
        if "top_up" in (t.get("url") or "") and t.get("type") == "page":
            return t
    req = urllib.request.Request(CDP + "/json/new?" + url, method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


class Session:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.mid = 0

    def ev(self, expr, await_promise=True):
        self.mid += 1
        self.ws.send(json.dumps({
            "id": self.mid,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise, "userGesture": True},
        }))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.mid:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return None
                return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def wait_for(s, expr, seconds, interval=0.4):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if s.ev(expr):
            return True
        time.sleep(interval)
    return False


def pick_amount(s, amount):
    """点金额档位；档位不在预设里就走「自定义」输入。"""
    if amount in TIERS:
        expr = r"""
        (() => {
          const hit = [...document.querySelectorAll('label.ds-radio-button')]
            .find(l => (l.innerText || '').trim() === '¥%d');
          if (!hit) return false;
          hit.click();
          return true;
        })()
        """ % amount
        for _ in range(16):
            if s.ev(expr):
                return True
            time.sleep(0.5)
        # 退一步：按 value 直接点 radio
        for _ in range(8):
            if s.ev(r"""
            (() => {
              const r = document.querySelector('input.ds-radio-button__input[value="%d"]');
              if (!r) return false;
              r.click();
              (r.closest('label') || r.parentElement || r).click();
              return true;
            })()
            """ % amount):
                return True
            time.sleep(0.5)
        return False
    # 自定义
    if not s.ev(r"""
    (() => {
      const hit = [...document.querySelectorAll('label.ds-radio-button')]
        .find(l => (l.innerText || '').trim() === '自定义');
      if (!hit) return false;
      hit.click();
      return true;
    })()
    """):
        return False
    time.sleep(0.4)
    return bool(s.ev(r"""
    (() => {
      const inp = [...document.querySelectorAll('input')]
        .find(i => i.type === 'text' || i.type === 'number' || i.type === '');
      if (!inp) return false;
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, '%d');
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      return true;
    })()
    """ % amount))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=int, required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not cdp_alive():
        launch_headless()

    tab = open_tab(TOPUP)
    s = Session(tab["webSocketDebuggerUrl"])
    try:
        # 每次重新加载，保证没有残留的旧支付弹窗
        s.ws.send(json.dumps({"id": 9001, "method": "Page.reload",
                              "params": {"ignoreCache": False}}))
        time.sleep(2)
        if not wait_for(s, "!!document.querySelector('label.ds-radio-button')", 25):
            body = s.ev("document.body.innerText.slice(0,300)") or ""
            if "登录" in body or "扫码" in body:
                raise RuntimeError("登录态失效，需要重新同步 Edge profile")
            raise RuntimeError("充值页没渲染出来")

        if not pick_amount(s, args.amount):
            raise RuntimeError(f"选不到金额 {args.amount}")
        time.sleep(0.5)

        if not s.ev(r"""
        (() => {
          const r = document.querySelector('input.ds-radio-card__input[value="wechat"]');
          if (!r) return false;
          r.click();
          const card = r.closest('label') || r.parentElement;
          if (card) card.click();
          return true;
        })()
        """):
            raise RuntimeError("选不到微信支付")

        time.sleep(0.5)
        if not s.ev(r"""
        (() => {
          const btn = [...document.querySelectorAll('div.ds-button')]
            .find(b => (b.innerText || '').trim() === '去支付');
          if (!btn) return false;
          btn.click();
          return true;
        })()
        """):
            raise RuntimeError("点不到「去支付」")

        # 等二维码 canvas 出现
        if not wait_for(s, "!!document.querySelector('canvas') && "
                            "document.body.innerText.indexOf('扫一扫付款') >= 0", 30):
            raise RuntimeError("支付二维码没出现")

        data = s.ev(r"""
        (() => {
          try {
            const c = document.querySelector('canvas');
            return c ? c.toDataURL('image/png') : 'NO_CANVAS';
          } catch (e) { return 'ERR:' + e.name; }
        })()
        """, await_promise=False)
        if not data or not str(data).startswith("data:image/png"):
            raise RuntimeError(f"导出二维码失败：{str(data)[:80]}")

        # 二维码只在支付页现场弹出（serve.py 缓存路径按 TTL 过期），
        # 落盘进系统临时目录，不在 recharge_page 里囤静态码文件
        out = args.out or os.path.join(
            tempfile.gettempdir(), f"ds_qr_{args.amount}_{os.getpid()}.png")
        raw = base64.b64decode(str(data).split(",", 1)[1])
        with open(out, "wb") as f:
            f.write(raw)

        if args.json:
            print(json.dumps({"ok": True, "amount": args.amount,
                              "file": out, "bytes": len(raw)}, ensure_ascii=False))
        else:
            print(out)
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        if "--json" in sys.argv:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                             ensure_ascii=False))
        else:
            print(f"失败：{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
