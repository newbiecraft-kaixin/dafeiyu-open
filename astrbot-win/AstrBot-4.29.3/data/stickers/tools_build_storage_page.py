# -*- coding: utf-8 -*-
"""生成《表情存储机制》可视化网页（自包含，无外部依赖）。

分区口径（用户钦定）：
  - 永久设定区：人工固定设定的素材（dafeiyu 手工命名）+ 官方黄脸通道（不占文件）
  - 自助下载区：自己下载/批量导入的（pool QQ缓存、dfy_new、dfy_web 早期批次）
  - 收藏自录区：机器人自己收藏入库的（fav，私发图→查重→问含义）
  - 归档区：淘汰静态帧（_static_archive，不参与发送）

页面放在 data/stickers/ 下（图片相对路径引用），配合 tools_source_server.py 打开。
"""
import io, json, os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
data = json.load(io.open(os.path.join(HERE, "_src_data.json"), encoding="utf-8"))
imgs = data["imgs"]

# fav 收藏库
fav_entries = []
for ln in io.open(os.path.join(HERE, "fav", "map_fav.txt"), encoding="utf-8"):
    ln = ln.strip()
    if ln and ":" in ln and not ln.startswith("#"):
        n, m, p = (ln.split(":", 2) + ["", ""])[:3]
        fav_entries.append({"name": n, "meaning": m, "path": p})

# 归档数（递归，含 pool/dfy_new/dfy_web 子目录）
arch_count = 0
for _root, _dirs, _files in os.walk(os.path.join(HERE, "_static_archive")):
    arch_count += sum(1 for f in _files if not f.startswith("."))

# map.txt 在役行数
map_lines = sum(
    1 for ln in io.open(os.path.join(HERE, "map.txt"), encoding="utf-8")
    if ln.strip() and ":" in ln and not ln.startswith("#")
)
manifest_lines = sum(
    1 for ln in io.open(os.path.join(HERE, "pool", "_manifest.txt"), encoding="utf-8")
    if ln.strip() and "\t" in ln
)

# 黄脸目录
fc = json.load(io.open(os.path.join(HERE, "..", "plugins", "astrbot_plugin_sticker_pack",
                                    "face_catalog.json"), encoding="utf-8"))
emoji_n = len(fc.get("emoji", []))
vis_n = len(fc.get("sysface_visible", []))
hid_n = len(fc.get("sysface_hidden", []))

dir_stat = Counter(r["dir"] for r in imgs)

payload = json.dumps({
    "imgs": imgs, "fav": fav_entries,
    "stat": {"dafeiyu": dir_stat.get("dafeiyu", 0), "dfy_new": dir_stat.get("dfy_new", 0),
             "dfy_web": dir_stat.get("dfy_web", 0), "pool": dir_stat.get("pool", 0),
             "fav": len(fav_entries), "arch": arch_count,
             "mapLines": map_lines, "manifest": manifest_lines,
             "emoji": emoji_n, "vis": vis_n, "hid": hid_n, "faces": data.get("faces", 290)},
}, ensure_ascii=False)

html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>大肥鱼表情库 · 存储机制</title>
<style>
:root{
  --bg:#15120c; --panel:#1f1a11; --panel2:#262013;
  --line:#3a3122; --gold:#c9a227; --gold-dim:#8a7326; --cyan:#4db8c4;
  --text:#e8dfc8; --muted:#9a8f74; --green:#5fae5f; --red:#c05b4d; --purple:#8f7ad6;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:"Microsoft YaHei","PingFang SC",sans-serif;line-height:1.6;padding:32px 18px 80px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:26px;color:var(--gold);letter-spacing:2px;margin-bottom:6px}
.sub{color:var(--muted);font-size:13px;margin-bottom:28px}
.sub b{color:var(--text)}
h2{font-size:18px;color:var(--gold);border-left:4px solid var(--gold-dim);padding-left:10px;margin:34px 0 14px}
p.note{font-size:13px;color:var(--muted);margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card .num{font-size:30px;font-weight:700;color:var(--gold)}
.card .lbl{font-size:12px;color:var(--muted);margin-top:2px}
.flow{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:10px;display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:13px}
.flow .step{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 14px;max-width:260px}
.flow .step b{color:var(--cyan)}
.flow .arr{color:var(--gold);font-size:18px}
.zones{display:grid;grid-template-columns:repeat(auto-fit,minmax(255px,1fr));gap:14px}
.zone{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--gold);border-radius:10px;padding:16px}
.zone h3{font-size:15px;margin-bottom:4px}
.zone .path{font-family:Consolas,monospace;font-size:12px;color:var(--cyan);word-break:break-all}
.zone .cnt{font-size:13px;margin:8px 0}
.zone .cnt b{font-size:17px;color:var(--gold)}
.zone .desc{font-size:12px;color:var(--muted)}
.zone .way{display:inline-block;margin-top:8px;padding:1px 9px;border-radius:10px;font-size:11px;color:#1a1508;font-weight:700}
.strip{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.strip img{width:44px;height:44px;object-fit:contain;border-radius:5px;background:#0e0c07;display:block}
.strip .more{width:44px;height:44px;display:flex;align-items:center;justify-content:center;border-radius:5px;background:var(--panel2);border:1px solid var(--line);font-size:11px;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--panel);border-radius:10px;overflow:hidden;font-size:13px}
th{background:var(--panel2);color:var(--gold);text-align:left;padding:10px 12px;font-size:12px}
td{padding:8px 12px;border-top:1px solid var(--line);vertical-align:top}
tr:hover td{background:#2a2415}
.mono{font-family:Consolas,monospace;font-size:11px;color:var(--muted);word-break:break-all}
.tag{display:inline-block;padding:1px 9px;border-radius:10px;font-size:11px;color:#1a1508;font-weight:700}
.foot{margin-top:30px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:16px}
a{color:var(--cyan)}
</style>
</head>
<body>
<div class="wrap">
  <h1>大肥鱼表情库 · 存储机制</h1>
  <div class="sub">数据快照 2026-10-29 · 根目录 <b>data/stickers/</b> · 相配套的来源溯源页 <a href="/">来源图谱</a></div>

  <div class="cards">
    <div class="card"><div class="num">__N_IMG__</div><div class="lbl">在役动图（四目录合计）</div></div>
    <div class="card"><div class="num">__N_FACES__</div><div class="lbl">官方黄脸（face 通道，零文件）</div></div>
    <div class="card"><div class="num">__N_FAV__</div><div class="lbl">收藏自录（fav/）</div></div>
    <div class="card"><div class="num">__N_ARCH__</div><div class="lbl">淘汰归档（_static_archive）</div></div>
  </div>

  <h2>一、运行时链路（开机 → 发图）</h2>
  <p class="note">插件 astrbot_plugin_sticker_pack 启动时把全部索引读进内存字典 stickers{}，之后发送只查内存，不再碰磁盘。</p>
  <div class="flow">
    <div class="step">插件启动<br><b>加载 3 份索引</b><br>map.txt（主库）<br>fav/map_fav.txt（收藏）<br>face_catalog.json（黄脸）</div><div class="arr">→</div>
    <div class="step">内存字典 <b>stickers{}</b><br>主库同名条目优先<br>fav 条目带 source=fav<br>新收藏有 1.12 倍新鲜度加成</div><div class="arr">→</div>
    <div class="step">AI 输出 <b>:名字:</b> 占位<br>或情绪打分自动选图<br>（rank 打分 + 32 语义组）</div><div class="arr">→</div>
    <div class="step">组装发送链<br>黄脸→Face 组件（官方通道）<br>图片→Image.fromFileSystem</div><div class="arr">→</div>
    <div class="step">单发闸门<br>30s 间隔 / 每分钟限额<br>26 次出图 25 种不重样</div>
  </div>

  <h2>二、四大存储区</h2>
  <p class="note">按「设定 / 下载 / 自录 / 归档」分区，物理目录互不掺和；发送时各区一起参与排名。</p>
  <div class="zones">
    <div class="zone" style="border-top-color:#c9a227">
      <h3>① 永久设定区</h3>
      <div class="path">stickers/dafeiyu/ ＋ 官方黄脸通道</div>
      <div class="cnt">手工动图 <b>__N_DAFEIYU__</b> 张 ＋ 黄脸 <b>__N_FACES__</b> 个</div>
      <div class="desc">人工固定设定：dafeiyu 桌面素材库手工整理命名（挥手.gif / 干饭.gif），每条名称和含义都是人写的；黄脸走 QQ 官方 face 通道（face_catalog.json：__N_EMOJI__ emoji，可见 __N_VIS__ / 隐藏 __N_HID__），不占任何文件。</div>
      <span class="way" style="background:#c9a227">人工设定 · 永久保留</span>
      <div class="strip" id="strip-dafeiyu"></div>
    </div>
    <div class="zone" style="border-top-color:#5fae5f">
      <h3>② 自助下载区</h3>
      <div class="path">stickers/pool/ ＋ dfy_new/ ＋ dfy_web/</div>
      <div class="cnt">合计 <b>__N_DL__</b> 张（pool __N_POOL__ / dfy_new __N_DFYNEW__ / dfy_web __N_DFYWEB__）</div>
      <div class="desc">自己批量下载导入：pool 是 10-28「图库扩充」从 QQ 缓存 emoji-recv 导入的 120 张（_manifest.txt 逐行记原始路径）；dfy_new / dfy_web 是早期网采编号批次（n202.gif / dfy_001.gif，批次记录未留 manifest）。</div>
      <span class="way" style="background:#5fae5f">批量下载 · 机器命名</span>
      <div class="strip" id="strip-dl"></div>
    </div>
    <div class="zone" style="border-top-color:#4db8c4">
      <h3>③ 收藏自录区</h3>
      <div class="path">stickers/fav/ ＋ fav/map_fav.txt</div>
      <div class="cnt">机器人自己收的 <b>__N_FAV__</b> 张</div>
      <div class="desc">运行中自助收藏：私发图或群聊缺图自动收 → 下载 → dHash 指纹查重（≥0.90 拒收）→ _pending 待确认（10 分钟超时作废）→ 问含义 → 追加 map_fav.txt → 立即进推荐。与主库物理隔离，主库一个字节不动。</div>
      <span class="way" style="background:#4db8c4">运行时自采 · 即收即用</span>
      <div class="strip" id="strip-fav"></div>
    </div>
    <div class="zone" style="border-top-color:#8a7326">
      <h3>④ 归档区（不发送）</h3>
      <div class="path">stickers/_static_archive/</div>
      <div class="cnt">淘汰静态帧 <b>__N_ARCH__</b> 张</div>
      <div class="desc">10-29 清理：238 张静态图（含 pool 静态帧）整体移入归档，文件一个没删；pool 归档 71 张的月份分布见来源图谱页。不加载、不参与发送，仅供溯源。</div>
      <span class="way" style="background:#8a7326">淘汰冷藏 · 保留可回滚</span>
    </div>
  </div>

  <h2>三、收藏入库流程（③ 区专属）</h2>
  <div class="flow">
    <div class="step">私发图片给机器人<br>或群里出现「缺图需求」<br>自动收群友发的图</div><div class="arr">→</div>
    <div class="step">下载到本地<br>算 <b>dHash 指纹</b><br>全库查重（≥0.90 拒收）</div><div class="arr">→</div>
    <div class="step">进 <b>fav/_pending</b> 待确认<br>_pending.json 记账<br>10 分钟没等到含义就作废</div><div class="arr">→</div>
    <div class="step">你回一句含义<br>「这是斗图必杀技」</div><div class="arr">→</div>
    <div class="step">改名去重 → 搬进 fav/<br>map_fav.txt 追加一行<br><b>名字:含义:fav/xxx.gif</b></div><div class="arr">→</div>
    <div class="step">内存字典立即更新<br>不用重启、不等重建<br>新图带 1.12 倍新鲜加成</div>
  </div>
  <p class="note">查重指纹缓存 fav/_dhash_v2.json；当前待确认区 _pending 为空 = 没有没收完的图。</p>

  <h2>四、索引文件一览</h2>
  <table>
    <thead><tr><th>文件</th><th>归属区</th><th>格式</th><th>条数</th><th>作用</th></tr></thead>
    <tbody>
      <tr><td class="mono">stickers/map.txt</td><td><span class="tag" style="background:#c9a227">主索引</span></td><td class="mono">名称:意思:相对路径</td><td>__N_MAP__ 行</td><td>全部在役图片（①+②区）的发送索引，# 开头是注释</td></tr>
      <tr><td class="mono">fav/map_fav.txt</td><td><span class="tag" style="background:#4db8c4">收藏</span></td><td class="mono">名称:意思:fav/xxx.ext</td><td>__N_FAV__ 行</td><td>③ 区独立索引，与主库物理分开但一起参与排名；主库同名优先</td></tr>
      <tr><td class="mono">pool/_manifest.txt</td><td><span class="tag" style="background:#5fae5f">下载</span></td><td class="mono">文件名\\t原始绝对路径</td><td>__N_MANI__ 行</td><td>pool 导入时的原始路径台账（溯源用，不参与运行）</td></tr>
      <tr><td class="mono">face_catalog.json</td><td><span class="tag" style="background:#c9a227">黄脸</span></td><td class="mono">JSON（emoji/可见/隐藏）</td><td>__N_EMOJI__ emoji</td><td>QQ 官方黄脸映射，sync_qq_faces.py 从 face_config.json 生成，① 区零文件部分</td></tr>
      <tr><td class="mono">fav/_dhash_v2.json</td><td><span class="tag" style="background:#4db8c4">收藏</span></td><td class="mono">JSON（名称→指纹）</td><td>—</td><td>收藏入库查重的指纹缓存，只入库时用，不参与推荐</td></tr>
      <tr><td class="mono">_src_data.json</td><td><span class="tag" style="background:#8a7326">文档</span></td><td class="mono">JSON</td><td>__N_IMG__ 条</td><td>来源图谱/本页的数据源（纯文档，插件不读）</td></tr>
    </tbody>
  </table>

  <div class="foot">
    说明：① 页面图片为相对路径引用，需放在 data/stickers/ 下打开。② 主库与收藏库发送时不分家——一起打分，只有入库时才分目录。
    ③ 想看每张图从哪来的（QQ 群/月份/原始路径）→ <a href="/">来源图谱</a>；来源识别错了可以直接在那页上改。
  </div>
</div>
<script>
const D = __PAYLOAD__;
const S = D.stat;

// 区域小图条
function strip(id, list){
  const el = document.getElementById(id);
  if(!el) return;
  const shown = list.slice(0, 10);
  for(const p of shown){
    el.insertAdjacentHTML('beforeend', `<img loading="lazy" src="${p}" onerror="this.style.opacity=.1">`);
  }
  if(list.length > shown.length){
    el.insertAdjacentHTML('beforeend', `<div class="more">+${list.length - shown.length}</div>`);
  }
  if(!list.length){
    el.insertAdjacentHTML('beforeend', `<div class="more">空</div>`);
  }
}
strip('strip-dafeiyu', D.imgs.filter(r=>r.dir==='dafeiyu').map(r=>`${r.dir}/${r.file}`));
strip('strip-dl', D.imgs.filter(r=>r.dir!=='dafeiyu').map(r=>`${r.dir}/${r.file}`));
strip('strip-fav', D.fav.map(f=>f.path));
</script>
</body>
</html>
"""

for k, v in {
    "__N_IMG__": len(imgs), "__N_FAV__": len(fav_entries), "__N_ARCH__": arch_count,
    "__N_FACES__": data.get("faces", 290), "__N_DAFEIYU__": dir_stat.get("dafeiyu", 0),
    "__N_DL__": dir_stat.get("dfy_new", 0) + dir_stat.get("dfy_web", 0) + dir_stat.get("pool", 0),
    "__N_POOL__": dir_stat.get("pool", 0), "__N_DFYNEW__": dir_stat.get("dfy_new", 0),
    "__N_DFYWEB__": dir_stat.get("dfy_web", 0), "__N_EMOJI__": emoji_n,
    "__N_VIS__": vis_n, "__N_HID__": hid_n, "__N_MAP__": map_lines,
    "__N_MANI__": manifest_lines,
}.items():
    html = html.replace(k, str(v))

out = os.path.join(HERE, "存储机制.html")
io.open(out, "w", encoding="utf-8").write(html.replace("__PAYLOAD__", payload))
print("written", out, os.path.getsize(out), "bytes")
