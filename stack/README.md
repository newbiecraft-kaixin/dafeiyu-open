# 大肥鱼 QQ 机器人（本机 Docker 部署）

- 来源：`whyever77/dafeiyu-astrbot`（clone 于 `..\repo-astrbot`，commit `dc16e74`）
- 组件：AstrBot `soulter/astrbot:v4.29.0`（自建镜像加 edge-tts / imageio-ffmpeg）+ NapCat `mlikiowa/napcat-docker:v4.18.37`
- 架构：QQ ⇄ NapCat ⇄ WebSocket(6199, token) ⇄ AstrBot ⇄ DeepSeek API

## 日常操作

```powershell
cd $env:USERPROFILE\dafeiyu\stack
docker compose up -d          # 启动（Docker Desktop 开机自启的前提下，电脑重启后跑这条即可）
docker compose ps             # 查看状态
docker logs -f --tail 50 dafeiyu-astrbot   # AstrBot 日志
docker logs -f --tail 50 dafeiyu-napcat    # NapCat / QQ 登录日志
docker compose down           # 停止
```

## 入口

| 用途 | 地址 | 凭据 |
|---|---|---|
| AstrBot 面板 | http://localhost:6185 | `astrbot` / `change-me-panel-password` |
| NapCat WebUI（扫码/重登/网络配置） | http://127.0.0.1:6099/webui | token 见 `napcat/config/webui.json` 的 `token` |
| OneBot v11 反向 WS | `ws://astrbot:6199/ws`（容器网络内） | token `change-me-6199-token` |

两个端口都只绑 `127.0.0.1`，不对局域网/公网暴露。

## 目录

- `data/`：AstrBot 数据（配置 `cmd_config.json`、插件、人格、日志），挂载到容器 `/AstrBot/data`
- `data/plugins/astrbot_plugin_face_convert/`：文字表情 → 真实 QQ 表情
- `napcat/config/`：NapCat 配置；`onebot11.json` 是模板，新登录的账号会自动继承（含反向 WS 与 token）
- `napcat/qq/`：QQ 登录态（`eqq` 目录），换机/重建容器需重新扫码
- `persona-大肥鱼.md`：官方人格原件；`persona-女仆强化.md`：追加的女仆设定段

## 已配置

- 模型源：DeepSeek `https://api.deepseek.com/v1`，模型 `deepseek-flash`（默认）/ `deepseek-v4-pro`（回退，纯文本）
- 默认人格：`karen`（凯伦风格；旧的大肥鱼官方人格 `dafeiyu` 仍保留，可随时切回）
- 平台适配器：`aiocqhttp`（QQ，反向 WS 6199，token 校验）
- 表情包插件：`astrbot_plugin_sticker_pack`，读取 `data/stickers/map.txt`，把回复里的 `:名称:` 换成本地图片；`/表情 名称` 可手动发
- 素材与索引仓库：`%USERPROFILE%\dafeiyu-assets`（头像、表情包、人格、插件源码，改动后跑 `scripts\sync-to-astrbot.ps1 -Restart` 同步）

## 未配置（按需再开）

- 百炼视觉/语音（图片转述、语音条、音色复刻）：需要 DashScope key，并套用仓库 `deploy/patches/` 的两个核心补丁
- 长期记忆 / 主动搭话：装社区插件 `livingmemory`、`proactive_chat`
- 访问控制白名单：当前为空（不做限制）；只想自己用就往 `platform_settings.id_whitelist` 里加 `qq:FriendMessage:<你的QQ>`

## 风险提示

NapCat 走的是非官方 QQ 协议，存在风控/封号概率，只用于小号；登录态与设备指纹绑定，重建容器后可能需重新扫码。
