# 桌宠（dsh-pet 0.4.1）

这台电脑上实际运行的桌宠就是 **dsh-pet**，它是 DeepSeek Harness（DSH）的桌宠插件，自带 109 个动作动画（webm）、气泡、聊天面板和 Electron 运行时。仓库里原先写的 clawd-on-desk 并不是实际在跑的那个壳。

## 安装

前置：已经装好并至少启动过一次 DeepSeek Harness（DSH），存在 `%USERPROFILE%\.dsh\profiles\web`。

> **DSH 目前未公开发布，没有官方下载地址，本仓库也不分发。** 没有 DSH 的机器暂时无法用这种方式装桌宠。

```powershell
powershell -ExecutionPolicy Bypass -File .\pet\dsh-pet\install.ps1
```

脚本做三件事：

1. `npm install ./pet/dsh-pet/dsh-pet-0.4.1.tgz`（安装包随仓库提供；万一本地缺失，脚本会从 Release 下载同版本；装进 `%USERPROFILE%\.dsh\profiles\web`，版本锁定 0.4.1）
2. `npm run ensure:electron`（下载 Electron 运行时）
3. 把 `dsh-pet` 写进该 profile 的 `dsh.profile.bundles`

装完重启 DSH，在插件里启用 dsh-pet 即可。个人数据目录在 `%USERPROFILE%\.dsh\dsh-pet`（配置、记忆、动作缓存）。

## 说明

- `dsh-pet-0.4.1.tgz` 是从本机 DSH 安装目录里取出的同款安装包（62.5 MB，1077 个文件），保证别人跑出来和本机一致。**安装包直接随仓库提供**，clone 后无需再下载；若文件被删掉，`install.ps1` 会自动从 Release 取同版本，也可以手动把 tgz 放回本目录。
- dsh-pet 自身以上游 MIT 许可发布（作者 PC2005-cloud，npm 包名 `dsh-pet`，首页 https://github.com/PC2005-cloud/dsh-pet ），二次分发请保留其 LICENSE。
- 首次安装时若 `%USERPROFILE%\.dsh\dsh-pet\main-config.jsonc` 不存在，脚本会自动铺入本目录的 `config.example.jsonc`（桌宠实例、人设/睡觉台词、模型、配图与动作、看屏、语音、泡泡、全局开关、物理、字体、工作状态台词、动画权重、周期事件，带中文注释，可自行修改）。已有旧配置则只补缺失的必填开关，不动原有设置。
- 配置合并是「顶层字段整段替换」：动了 `animations` / `memes` / `physics` / `workStatusTexts` 这类整段，要把整段写全，别只写一半；没写的顶层字段才回落包内默认值（`assets/config.jsonc`）。
- 更新版本：`npm install dsh-pet@latest --save-exact` 装回 DSH profile 即可，不需要动本目录的 tgz（除非要同步仓库）。

## 海草 / 背景泡泡不显示

这两层是同一套背景层（`bubbleBackdropEnabled`），在另一台机器上装完却没显示，按顺序查：

```powershell
# 1) 看当前配置和 dsh-pet 版本（只读）
.\pet\dsh-pet\install.ps1 -Backdrop check

# 2) 打开背景层（数量为 0 会一起改成 9，非法取值会拉回默认）
.\pet\dsh-pet\install.ps1 -Backdrop on
```

- 版本低于 **0.4.1** 的 dsh-pet 没有这层渲染代码，改配置也不会出现，需要重跑完整 `install.ps1`
- `bubbleBackdropEnabled` 为 `false` 时整层隐藏；`bubbleBackdropCount` 为 0 只关泡泡，海草仍在
- 配置在 `%USERPROFILE%\.dsh\dsh-pet\main-config.jsonc`，脚本只改这几个字段，注释和其他设置不动
- 桌宠每 30 秒拉一次配置；等半分钟不生效，右键桌宠重启一次

## 主气泡背景板 / 壁纸

主气泡（碎碎念、对话回复、工作状态那句）可以加一层背景板，默认关闭。配置写在
`%USERPROFILE%\.dsh\dsh-pet\main-config.jsonc`，键名与默认值见 `config.example.jsonc`：

- `bubbleBoardEnabled`：总开关，默认 `false`；关掉就回到原来的白底气泡
- `bubbleBoardPreset`：`glass`（玻璃）/ `paper`（纸张）/ `night`（暗夜）/ `custom`（自定义）
- `bubbleBoardMode`：`custom` 下选 `solid`（纯色）/ `gradient`（渐变）/ `image`（图片壁纸）
- `bubbleBoardColor`、`bubbleBoardGradientFrom`、`bubbleBoardGradientTo`：纯色/渐变颜色
- `bubbleBoardImage`：壁纸地址，支持 http(s) URL、`data:` 和本地绝对路径（如 `C:\pics\wall.jpg`）
- `bubbleBoardScrim`：壁纸遮罩强度 `0–0.8`，深色壁纸配亮字时靠它保证文字对比度
- `bubbleBoardText`：`auto`（按预设/壁纸自动）/ `light`（亮字）/ `dark`（暗字）

回退：把 `bubbleBoardEnabled` 改回 `false`（或整段删掉，包内默认就是关）；余额纯文字态、
待办面板不受这个开关影响，仍保持原样。
