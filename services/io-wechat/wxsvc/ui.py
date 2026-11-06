"""可视化页：浏览器打开 /ui（或 /）时的实时状态面板。

页面只读，数据全部来自本服务的 HTTP 接口：
    /health    附着状态、收发计数、事件队列
    /messages  增量来消息（2 秒轮询）
    /sessions  会话列表（仅首屏与手动刷新，UIA 读取不适合高频调用）
"""

from __future__ import annotations

from infra import Html

DASHBOARD_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>io-wechat · 状态面板</title><style>
  :root{--bg:#070a14;--panel:#101731;--line:rgba(122,152,222,.18);--text:#e9edff;
        --muted:#8b97bd;--dim:#636d95;--acc:#6b8fff;--good:#3ddc97;--bad:#ff617b}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;color:var(--text);font:14px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;
       background:radial-gradient(900px 520px at 10% -12%,rgba(107,143,255,.18),transparent 60%),
                  linear-gradient(180deg,#0c1122,#070a14 46%)}
  .wrap{max-width:1080px;margin:0 auto;padding:26px 20px 46px}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;
         padding-bottom:14px;border-bottom:1px solid var(--line)}
  h1{margin:0;font-size:19px}
  .sub{color:var(--muted);font-size:12.5px}
  .pills{display:flex;gap:8px;flex-wrap:wrap}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);
        color:var(--muted);font-family:Consolas,monospace}
  .pill.good{color:#b9f5dd;border-color:rgba(61,220,151,.4);background:rgba(61,220,151,.1)}
  .pill.bad{color:#ffc2cd;border-color:rgba(255,97,123,.4);background:rgba(255,97,123,.1)}
  .banner{margin-top:16px;padding:10px 14px;border-radius:10px;font-size:13px;
          border:1px solid var(--line);background:rgba(122,152,222,.06)}
  .banner.good{color:#b9f5dd;border-color:rgba(61,220,151,.35)}
  .banner.bad{color:#ffc2cd;border-color:rgba(255,97,123,.35)}
  .nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:18px}
  .num{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:rgba(16,23,49,.6)}
  .num b{display:block;font-size:22px;line-height:1.2}
  .num small{color:var(--dim);font-size:12px}
  h3{margin:24px 0 8px;font-size:13px;letter-spacing:1px;color:#c8d3ff}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
  thead th{color:var(--dim);font-weight:600;font-size:12px}
  .chip{display:inline-block;margin:3px 6px 3px 0;padding:2px 10px;border-radius:999px;
        border:1px solid var(--line);background:rgba(122,152,222,.08);
        font-family:Consolas,monospace;font-size:12.5px;color:#cfdcff}
  .note{color:var(--dim);font-size:12px;margin:8px 0 0}
  .mono{font-family:Consolas,monospace;color:#bfd0ff}
  footer{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);
         color:var(--dim);font-size:12px;display:flex;gap:16px;flex-wrap:wrap;justify-content:space-between}
  a{color:#9fbcff;text-decoration:none}
  a:hover{text-decoration:underline}
  button{font:inherit;color:#cfdcff;background:rgba(122,152,222,.12);border:1px solid var(--line);
         border-radius:999px;padding:3px 12px;cursor:pointer}
  button:hover{border-color:rgba(107,143,255,.6)}
</style></head><body><div class="wrap">
<header>
  <div><h1>io-wechat</h1><div class="sub">PC 微信 4.x · wechatauto RPA · 端口 23392</div></div>
  <div class="pills">
    <span id="attach" class="pill">附着状态 …</span>
    <span id="bot" class="pill">机器人 …</span>
    <span id="uptime" class="pill">运行 …</span>
  </div>
</header>

<div id="banner" class="banner">正在读取 /health …</div>

<div class="nums">
  <div class="num"><b id="received">--</b><small>收到消息</small></div>
  <div class="num"><b id="sent">--</b><small>已发送</small></div>
  <div class="num"><b id="groups">--</b><small>群消息</small></div>
  <div class="num"><b id="duplicate">--</b><small>重复过滤</small></div>
  <div class="num"><b id="blocked">--</b><small>夜间拦截</small></div>
  <div class="num"><b id="pushed">--</b><small>入队事件</small></div>
  <div class="num"><b id="queued">--</b><small>队列待取</small></div>
  <div class="num"><b id="dropped">--</b><small>溢出丢弃</small></div>
</div>

<h3>会话（UIA 实读，可手动刷新） <button id="reload" type="button">刷新会话</button></h3>
<div id="sessions"><p class="note">读取中 …</p></div>

<h3>最近消息 <span class="note" id="cursor">seq 0</span></h3>
<table><thead><tr><th style="width:88px">时间</th><th style="width:150px">会话</th>
<th style="width:130px">发送者</th><th>正文</th></tr></thead>
<tbody id="msgs"><tr><td colspan="4" class="note">暂无消息，等待微信来消息 …</td></tr></tbody></table>

<footer>
  <span id="stamp">--</span>
  <span>夜间 22:00–08:00 禁止主动消息 · 桌面让路开启 · 每 2 秒自动刷新</span>
  <a href="http://127.0.0.1:23408/ui">← 返回中枢</a>
</footer>
</div>
<script>
const state = { cursor: 0, booted: false };
const $ = (id) => document.getElementById(id);
const esc = (t) => String(t == null ? '' : t).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function setNum(id, value) { $(id).textContent = (value == null ? '--' : value); }
function fmtUptime(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return (h ? h + ' 小时 ' : '') + m + ' 分';
}

async function refreshHealth() {
  try {
    const health = await fetch('/health', { cache: 'no-store' }).then((r) => r.json());
    const attached = !!health.ok;
    const stats = health.stats || {};
    $('attach').textContent = attached ? '微信已附着' : '微信未附着';
    $('attach').className = 'pill ' + (attached ? 'good' : 'bad');
    $('bot').textContent = '机器人 ' + (health.bot || '（未知昵称）');
    $('uptime').textContent = '运行 ' + fmtUptime(health.uptime);
    setNum('received', stats.received);
    setNum('sent', stats.sent);
    setNum('groups', stats.groups);
    setNum('duplicate', stats.duplicate);
    setNum('blocked', stats.blocked_night);
    const queue = health.event_queue || {};
    setNum('pushed', queue.pushed);
    setNum('queued', queue.queued);
    setNum('dropped', queue.dropped);
    const banner = $('banner');
    if (attached) {
      banner.className = 'banner good';
      banner.textContent = '链路正常：wechatauto 已附着，来消息进 /events，AstrBot 通过 /messages 长轮询消费。';
    } else {
      banner.className = 'banner bad';
      banner.textContent = '微信未附着：请确认微信已登录且窗口可见，服务会每 5 秒自动重试。';
    }
    $('stamp').textContent = '刷新于 ' + new Date().toLocaleTimeString();
  } catch (error) {
    $('banner').className = 'banner bad';
    $('banner').textContent = '读取 /health 失败：' + error.message;
  }
}

async function refreshMessages() {
  try {
    const data = await fetch('/messages?since=' + state.cursor + '&wait=0', { cache: 'no-store' })
      .then((r) => r.json());
    const seq = data.seq || 0;
    const items = data.messages || [];
    state.cursor = Math.max(state.cursor, seq);
    $('cursor').textContent = 'seq ' + state.cursor;
    if (!items.length) return;
    const tbody = $('msgs');
    if (!state.booted) { tbody.innerHTML = ''; state.booted = true; }
    const rows = items.slice(-30).map((m) => {
      const when = m.ts ? new Date(m.ts * 1000).toLocaleTimeString() : '--';
      return '<tr><td class="mono">' + esc(when) + '</td><td>' +
        esc(m.chat || m.chat_uid || '') + '</td><td>' + esc(m.sender || '') +
        '</td><td>' + esc(m.content || '') + '</td></tr>';
    }).join('');
    tbody.insertAdjacentHTML('beforeend', rows);
    while (tbody.children.length > 60) tbody.removeChild(tbody.firstChild);
  } catch (error) { /* 长轮询偶发失败不打扰页面 */ }
}

async function refreshSessions() {
  const box = $('sessions');
  box.innerHTML = '<p class="note">读取中 …</p>';
  try {
    const data = await fetch('/sessions', { cache: 'no-store' }).then((r) => r.json());
    const names = (data.data || {}).sessions || [];
    box.innerHTML = names.length
      ? names.map((n) => '<span class="chip">' + esc(n) + '</span>').join('')
      : '<p class="note">没有会话（微信未附着或列表为空）</p>';
  } catch (error) {
    box.innerHTML = '<p class="note">读取失败：' + esc(error.message) + '</p>';
  }
}

$('reload').addEventListener('click', refreshSessions);
refreshHealth();
refreshMessages();
refreshSessions();
setInterval(refreshHealth, 2000);
setInterval(refreshMessages, 2000);
</script></body></html>"""


def handle_ui(_req) -> tuple[int, Html]:
    return 200, Html(DASHBOARD_HTML)
