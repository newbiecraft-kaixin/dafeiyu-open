
## 2026-11-09 桌宠图片推送失效
- 现象：群聊图片不再显示为桌宠气泡图片，退化为纯文字/http
- 根因：清理缓存时删掉 bots/pet-images 目录，dafeiyu_pet._cache_local 复制图片时目标目录不存在直接抛异常返回空
- 处理：_cache_local 复制前 mkdir(parents=True, exist_ok=True) 自愈重建；手动建回目录并同步运行副本（MD5 一致）
- 教训：缓存目录可删但代码必须能自愈重建，不能假设目录常驻

## 2026-11-09 声线切换失败（QQ+桌宠全断）
- 现象：桌宠右键/网页声线管理切换报错，QQ 端 /声线 同样异常
- 根因：cosyvoice_ref 归拢进 audio/ 包时漏改三处解析点——hub _voices.py VOICE_ROOT、hub petctl.py PROFILES、basic_tools voice_engine._find_ref_dir，全部还指向旧平铺根，声线档案解析为空、.active 写入失败
- 处理：三处均改为 audio/ 优先 + 旧位置兜底的双候选解析；voice_engine 已同步运行副本
- 教训：目录归拢必须全仓 grep 资产名引用点，不能只改启动脚本和 infra 约定位置

## 2026-11-09 唱歌翻唱全部失败（转换后没有人声 RMS 0.0000）
- 现象：/听歌 或自动翻唱报「转换后没有人声（RMS 0.0000）」，成功的全是旧分轨缓存命中曲目（番茄狂想曲）
- 排障：手动复现全链路 → demucs 分轨的人声轨 -47.6dB（正常 -17.4dB）→ 先误判 torch 2.14 升级破坏 demucs，换 seedvc_env（torch 2.7.1+cu128）重跑分轨依旧静音 → 排除环境问题 → faster-whisper 全曲扫《开心向前冲》：7 段全部只转写出「字幕by索兰娅」，原曲根本没有原唱人声
- 根因：网易云外链对没版权/下架的歌会返回假音源（伴奏或带「字幕byxxx」水印的视频音轨），扒歌函数不验货直接入库；demucs 正确地分离出「没有人声」，Seed-VC 检查 RMS 拦截
- 处理：1) helpers._netease_fetch_song 下载后加验货 _song_lyrics_probe——抽 25%/55% 两窗各 15s 用本地 faster-whisper small 转写，有效字数<8 或含「字幕by」即拒收删文件换下一首；2) demucs 调用固定用 seedvc_env python（随翻唱环境走，不跟 AstrBot 运行时解释器的 torch 升级漂移），demucs 已装入 seedvc_env；3) 删除曲库里已入库的假歌
- 教训：外链下载的内容必须验货再入库，响度/时长正常不代表是真歌；排障时「同一首歌反复成功」是缓存假象，要拿新歌复现；修 bug 前的假设（torch 升级破坏）要被复现实验证伪后立刻修正，不能顺着修
