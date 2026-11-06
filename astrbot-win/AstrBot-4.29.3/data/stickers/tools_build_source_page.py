# -*- coding: utf-8 -*-
"""生成《表情库来源图谱》可视化网页（自包含，无外部依赖）。
数据源：_src_data.json（map.txt × pool/_manifest.txt 交叉结果）。
图片用相对路径引用，页面必须放在 data/stickers/ 目录下才能预览动图。

v2 新增：
  - 行内编辑：名称/含义/月份/类型/原始路径 可直接改，经 tools_source_server.py 持久化
    写回 _src_data.json，并同步 map.txt（名称:意思:路径）。
  - 月份区间筛选：支持 pool 渠道的按月过滤，可含/排除无日期项。
  - file:// 直接打开时为只读模式（页面顶部给出提示）。
"""
import io, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
data = json.load(io.open(os.path.join(HERE, "_src_data.json"), encoding="utf-8"))

# 归档 pool 的月份分布（_static_archive/pool × manifest）
MANI = {}
for ln in io.open(os.path.join(HERE, "pool", "_manifest.txt"), encoding="utf-8"):
    s = ln.strip()
    if "\t" in s:
        f, p = s.split("\t", 1)
        MANI[os.path.splitext(f)[0]] = p.replace("\\", "/")

from collections import Counter
arch_m = Counter()
for f in os.listdir(os.path.join(HERE, "_static_archive", "pool")):
    src = MANI.get(os.path.splitext(f)[0], "")
    m = [x for x in src.split("/") if len(x) == 7 and x.startswith("20")]
    arch_m[m[0] if m else "未知"] += 1

DIR_META = {
    "dafeiyu": {"label": "dafeiyu 手工素材", "origin": "桌面素材库 dafeiyu-assets/stickers/dafeiyu（人工整理命名，如 挥手.gif）", "color": "#c9a227"},
    "dfy_new": {"label": "dfy_new 批次", "origin": "早期扩充批次（n202.gif 等编号命名，批次记录未留 manifest）", "color": "#4db8c4"},
    "dfy_web": {"label": "dfy_web 批次", "origin": "早期网采批次（dfy_001.gif 等编号命名，批次记录未留 manifest）", "color": "#8f7ad6"},
    "pool": {"label": "pool QQ缓存导入", "origin": "QQ 客户端自动缓存 emoji-recv（10-28「图库扩充」批量导入，_manifest.txt 记录原始路径）", "color": "#5fae5f"},
}

imgs = data["imgs"]
dir_stat = Counter(r["dir"] for r in imgs)
pool_months = Counter(r.get("month") or "未知" for r in imgs if r["dir"] == "pool")
months = sorted(set(list(pool_months) + list(arch_m) + ["2025-10"]))
months = [m for m in months if m >= "2025-10"]

payload = json.dumps({"imgs": imgs, "dirMeta": DIR_META, "dirStat": dict(dir_stat),
                      "poolMonths": dict(pool_months), "archMonths": dict(arch_m),
                      "months": months, "faces": data["faces"]}, ensure_ascii=False)

html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>大肥鱼表情库 · 来源图谱</title>
<style>
:root{
  --bg:#15120c; --panel:#1f1a11; --panel2:#262013;
  --line:#3a3122; --gold:#c9a227; --gold-dim:#8a7326; --cyan:#4db8c4;
  --text:#e8dfc8; --muted:#9a8f74; --green:#5fae5f; --red:#c05b4d;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:"Microsoft YaHei","PingFang SC",sans-serif;line-height:1.6;padding:32px 18px 80px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:26px;color:var(--gold);letter-spacing:2px;margin-bottom:6px}
.sub{color:var(--muted);font-size:13px;margin-bottom:28px}
.sub b{color:var(--text)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:34px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card .num{font-size:30px;font-weight:700;color:var(--gold)}
.card .lbl{font-size:12px;color:var(--muted);margin-top:2px}
h2{font-size:18px;color:var(--gold);border-left:4px solid var(--gold-dim);padding-left:10px;margin:34px 0 14px}
p.note{font-size:13px;color:var(--muted);margin-bottom:14px}
.chan{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:14px}
.ch{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--gold);border-radius:10px;padding:16px}
.ch h3{font-size:15px;margin-bottom:6px}
.ch .dir{font-family:Consolas,monospace;font-size:12px;color:var(--cyan)}
.ch .cnt{font-size:13px;margin:8px 0}
.ch .cnt b{color:var(--gold);font-size:17px}
.ch .cnt .arch{color:var(--muted)}
.ch .origin{font-size:12px;color:var(--muted)}
.flow{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:10px;display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:13px}
.flow .step{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 14px;max-width:250px}
.flow .step b{color:var(--cyan)}
.flow .arr{color:var(--gold);font-size:18px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:22px}
.bars{display:flex;align-items:flex-end;gap:10px;height:190px;padding-top:8px}
.bar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:0;height:100%}
.bar .col{width:100%;max-width:52px;display:flex;flex-direction:column;justify-content:flex-end}
.bar .seg-act{background:var(--gold);border-radius:3px 3px 0 0}
.bar .seg-arch{background:#4a3f24;border-radius:3px 3px 0 0}
.bar .v{font-size:11px;color:var(--text);margin-bottom:2px}
.bar .m{font-size:10px;color:var(--muted);margin-top:6px;white-space:nowrap}
.legend{display:flex;gap:18px;margin-top:14px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.btn{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:6px 16px;border-radius:20px;cursor:pointer;font-size:13px}
.btn.on{background:var(--gold);color:#1a1508;border-color:var(--gold);font-weight:700}
input[type=search]{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:7px 14px;border-radius:20px;font-size:13px;width:200px;outline:none}
input[type=search]:focus{border-color:var(--gold-dim)}
select{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:6px 10px;border-radius:20px;font-size:13px;outline:none;cursor:pointer}
select:focus{border-color:var(--gold-dim)}
.flabel{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px}
.flabel input[type=checkbox]{accent-color:var(--gold);cursor:pointer}
#saveChip{font-size:12px;padding:3px 12px;border-radius:12px;border:1px solid var(--line);color:var(--muted)}
#saveChip.ok{color:var(--green);border-color:var(--green)}
#saveChip.err{color:var(--red);border-color:var(--red)}
#saveChip.busy{color:var(--cyan);border-color:var(--cyan)}
table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:10px;overflow:hidden;font-size:13px}
th{background:var(--panel2);color:var(--gold);text-align:left;padding:10px 12px;font-size:12px;position:sticky;top:0}
td{padding:8px 12px;border-top:1px solid var(--line);vertical-align:middle}
tr:hover td{background:#2a2415}
td img{width:52px;height:52px;object-fit:contain;border-radius:6px;background:#0e0c07;display:block}
.tag{display:inline-block;padding:1px 9px;border-radius:10px;font-size:11px;color:#1a1508;font-weight:700}
.mono{font-family:Consolas,monospace;font-size:11px;color:var(--muted);word-break:break-all}
.foot{margin-top:30px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:16px}
.empty{color:var(--muted);text-align:center;padding:30px}
#banner{display:none;background:#3a3118;border:1px solid var(--gold-dim);border-radius:10px;padding:12px 18px;font-size:13px;color:var(--text);margin-bottom:20px}
td[contenteditable]{cursor:text}
td[contenteditable]:hover{box-shadow:inset 0 0 0 1px var(--line);border-radius:4px}
td[contenteditable]:focus{outline:2px solid var(--gold-dim);background:#2a2415;border-radius:4px}
.edithint{font-size:12px;color:var(--gold-dim);margin-bottom:10px}
</style>
</head>
<body>
<div class="wrap">
  <h1>大肥鱼表情库 · 来源图谱</h1>
  <div class="sub">生成于 2026-10-29 · 数据源 <b>map.txt</b> × <b>pool/_manifest.txt</b> × <b>_static_archive/</b> · 图片为相对路径引用，本页需放在 data/stickers/ 下打开</div>

  <div id="banner">⚠ 静态只读模式（未检测到本地服务）：编辑不可用。请在本目录运行 <b>python tools_source_server.py</b> 后通过 <b>http://127.0.0.1:8712/</b> 打开本页。</div>

  <div class="cards">
    <div class="card"><div class="num">290</div><div class="lbl">QQ 黄脸（face 通道）</div></div>
    <div class="card"><div class="num">97</div><div class="lbl">动图表情（在役）</div></div>
    <div class="card"><div class="num">238</div><div class="lbl">静态图（已归档 _static_archive）</div></div>
    <div class="card"><div class="num">4</div><div class="lbl">来源渠道</div></div>
    <div class="card"><div class="num">5</div><div class="lbl">群聊收藏（fav/_pending）</div></div>
  </div>

  <h2>一、四个来源渠道</h2>
  <p class="note">表情库按目录区分批次；pool 有 _manifest.txt 逐行记录原始路径，其余三批为早期导入、批次记录缺失。</p>
  <div class="chan" id="chans"></div>

  <h2>二、capoo（猫猫虫咖波）表情的完整链路</h2>
  <div class="flow">
    <div class="step">机器人所在群 <b>586864126</b><br>「猫猫虫咖波爱好者交流」</div><div class="arr">→</div>
    <div class="step">群友天天发 capoo 表情<br>QQ 收到即自动缓存</div><div class="arr">→</div>
    <div class="step"><b>emoji-recv/2025-10 ~ 2026-10/</b><br>Documents\\Tencent Files\\1336312276\\nt_qq\\nt_data</div><div class="arr">→</div>
    <div class="step">10-28「图库扩充」批量导入<br>120 张 → data/stickers/pool/</div><div class="arr">→</div>
    <div class="step">在役 <b>47</b> 张（全为 Ori 原图）<br>归档 <b>71</b> 张（静态帧）</div>
  </div>
  <p class="note">结论：capoo 多不是异常 —— 群里发的表情基本全是 capoo，QQ 缓存自然堆满；无人手动放入。</p>

  <h2>三、pool 渠道按月分布（QQ 缓存入库时间）</h2>
  <div class="chart">
    <div class="bars" id="bars"></div>
    <div class="legend"><span><i style="background:var(--gold)"></i>在役（动图可用）</span><span><i style="background:#4a3f24"></i>归档（静态帧淘汰）</span></div>
  </div>

  <h2>四、在役动图明细（97 张）</h2>
  <p class="edithint" id="edithint"></p>
  <div class="toolbar">
    <button class="btn on" data-d="all">全部</button>
    <button class="btn" data-d="dafeiyu">dafeiyu</button>
    <button class="btn" data-d="dfy_new">dfy_new</button>
    <button class="btn" data-d="dfy_web">dfy_web</button>
    <button class="btn" data-d="pool">pool</button>
    <label class="flabel">入库 <select id="mFrom"></select></label>
    <label class="flabel">~ <select id="mTo"></select></label>
    <label class="flabel"><input type="checkbox" id="mUnk" checked>含无日期</label>
    <input type="search" id="q" placeholder="搜名称 / 含义 / 月份…">
    <span id="saveChip">—</span>
    <span class="sub" id="stat" style="margin:0"></span>
  </div>
  <table>
    <thead><tr><th>预览</th><th>名称</th><th>含义</th><th>渠道</th><th>来源信息</th><th>原始路径 / 文件</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>

  <div class="foot">
    说明：① 静态图未删除，整体移至 <span class="mono">_static_archive/</span>，map.txt 备份 <span class="mono">map.txt.bak_static_20261029_141136</span>。
    ② pool 中 47 个假扩展名文件（jpg/png 实为 GIF）已由 fix_gif_ext.py 改回 .gif。
    ③ 290 张黄脸走 QQ 官方 face 通道（face_config.json 同步），不占图片库。
    ④ 行内编辑保存时自动写回 _src_data.json 并同步 map.txt（机器人需重载插件后生效）；每次保存前滚动备份 *.bak_edit。
  </div>
</div>
<script>
let D = __PAYLOAD__;
const TAGC = {dafeiyu:'#c9a227', dfy_new:'#4db8c4', dfy_web:'#8f7ad6', pool:'#5fae5f'};
let SERVER = false;

// ---------- 数据加载：优先本地服务（可编辑），失败则用内嵌数据（只读） ----------
async function boot(){
  try{
    const r = await fetch('/api/data', {cache:'no-store'});
    if(r.ok){ const j = await r.json(); D.imgs = j.imgs; SERVER = true; }
  }catch(e){}
  document.getElementById('banner').style.display = SERVER ? 'none' : 'block';
  document.getElementById('edithint').textContent = SERVER
    ? '✏ 单元格可直接点击编辑（名称 / 含义 / 月份 / 类型 / 原始路径），失焦自动保存并同步 map.txt。'
    : '当前为只读模式。';
  buildMonthSelects(); render();
}

// ---------- 渠道卡 ----------
(function(){
  const ch = document.getElementById('chans');
  for (const [dir, meta] of Object.entries(D.dirMeta)) {
    const n = D.dirStat[dir] || 0;
    const arch = dir==='pool' ? 71 : (dir==='dfy_new' ? 88 : dir==='dfy_web' ? 79 : 0);
    ch.insertAdjacentHTML('beforeend',
      `<div class="ch" style="border-top-color:${meta.color}">
        <h3>${meta.label}</h3><div class="dir">${dir}/</div>
        <div class="cnt">在役 <b>${n}</b> 张 <span class="arch">· 归档 ${arch} 张</span></div>
        <div class="origin">${meta.origin}</div></div>`);
  }
  const bars = document.getElementById('bars');
  const pm = D.poolMonths, am = D.archMonths;
  const maxV = Math.max(...D.months.map(m => (pm[m]||0) + (am[m]||0)), 1);
  for (const m of D.months) {
    const a = pm[m]||0, r = am[m]||0;
    bars.insertAdjacentHTML('beforeend',
      `<div class="bar"><div class="col">
        <div class="v">${a+r || ''}</div>
        <div class="seg-arch" style="height:${r/maxV*140}px"></div>
        <div class="seg-act" style="height:${a/maxV*140}px"></div>
      </div><div class="m">${m.slice(2)}</div></div>`);
  }
})();

// ---------- 月份筛选 ----------
function allMonths(){
  const s = new Set(D.months);
  for (const r of D.imgs) if (r.month) s.add(r.month);
  return [...s].sort();
}
function buildMonthSelects(){
  const ms = allMonths();
  const opts = ['<option value="all">不限</option>'].concat(ms.map(m=>`<option value="${m}">${m}</option>`));
  for (const id of ['mFrom','mTo']) {
    const el = document.getElementById(id);
    el.innerHTML = opts.join(''); el.value = 'all';
    el.onchange = render;
  }
  document.getElementById('mUnk').onchange = render;
}

// ---------- 明细表 ----------
let curD = 'all';
const rows = document.getElementById('rows'), stat = document.getElementById('stat'), chip = document.getElementById('saveChip');
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function chipState(t, cls){ chip.textContent = t; chip.className = cls || ''; }
let saveSeq = 0;

function render(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const f = document.getElementById('mFrom').value, t = document.getElementById('mTo').value;
  const incUnk = document.getElementById('mUnk').checked;
  rows.innerHTML = '';
  let n = 0;
  for (let i = 0; i < D.imgs.length; i++) {
    const r = D.imgs[i];
    if (curD !== 'all' && r.dir !== curD) continue;
    if (r.month) {
      if (f !== 'all' && r.month < f) continue;
      if (t !== 'all' && r.month > t) continue;
    } else if (!incUnk) continue;
    const hay = (r.name + r.desc + (r.month||'') + (r.kind||'') + r.file + (r.src||'')).toLowerCase();
    if (q && !hay.includes(q)) continue;
    n++;
    const ed = SERVER ? ' contenteditable="plaintext-only"' : ' contenteditable="false"';
    const srcCell = r.dir === 'pool'
      ? `<span data-f="month"${ed}>${esc(r.month||'')}</span> · <span data-f="kind"${ed}>${esc(r.kind||'')}</span>`
      : '批次导入（无 manifest）';
    const srcPath = SERVER
      ? `<span data-f="src"${ed}>${esc(r.src || (r.dir + '/' + r.file))}</span>`
      : esc(r.src || (r.dir + '/' + r.file));
    rows.insertAdjacentHTML('beforeend',
      `<tr data-i="${i}"><td><img loading="lazy" src="${r.dir}/${r.file}" onerror="this.style.opacity=.15"></td>
       <td data-f="name"${ed}><b>${esc(r.name)}</b></td><td data-f="desc"${ed}>${esc(r.desc)}</td>
       <td><span class="tag" style="background:${TAGC[r.dir]}">${r.dir}</span></td>
       <td>${srcCell}</td>
       <td class="mono">${srcPath}</td></tr>`);
  }
  if (!n) rows.innerHTML = '<tr><td colspan="6" class="empty">没有匹配的表情</td></tr>';
  stat.textContent = `显示 ${n} / ${D.imgs.length} 张`;
}
document.querySelectorAll('.btn').forEach(b => b.onclick = () => {
  document.querySelectorAll('.btn').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); curD = b.dataset.d; render();
});
document.getElementById('q').oninput = render;

// ---------- 行内编辑保存 ----------
rows.addEventListener('focusout', async e => {
  const cell = e.target.closest && e.target.closest('td[data-f], span[data-f]');
  if (!cell || !SERVER) return;
  const tr = cell.closest('tr'); if (!tr) return;
  const rec = D.imgs[+tr.dataset.i]; if (!rec) return;
  const fields = {};
  tr.querySelectorAll('[data-f]').forEach(c => {
    const v = c.textContent.trim();
    const key = c.dataset.f;
    if ((rec[key]||'') !== v && !(key==='src' && !rec.src && v === rec.dir+'/'+rec.file)) fields[key] = v;
  });
  if (!Object.keys(fields).length) return;
  const seq = ++saveSeq;
  chipState('保存中…', 'busy');
  try{
    const res = await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({dir: rec.dir, file: rec.file, fields})});
    const j = await res.json();
    if (!res.ok || !j.ok) throw new Error(j.err || res.status);
    Object.assign(rec, j.record || fields);
    chipState('已保存 ' + new Date().toLocaleTimeString(), 'ok');
  }catch(err){
    chipState('保存失败：' + err.message, 'err');
    render(); // 回滚显示
  }
});
rows.addEventListener('keydown', e => {
  const cell = e.target.closest && e.target.closest('td[data-f], span[data-f]');
  if (cell && e.key === 'Enter'){ e.preventDefault(); cell.blur(); }
});

boot();
</script>
</body>
</html>
"""

out = os.path.join(HERE, "来源图谱.html")
io.open(out, "w", encoding="utf-8").write(html.replace("__PAYLOAD__", payload))
print("written", out, os.path.getsize(out), "bytes")
