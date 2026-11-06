# 桌宠（dsh-pet 0.4.1）

这台电脑上实际运行的桌宠就是 **dsh-pet**，它是 DeepSeek Harness（DSH）的桌宠插件，自带 109 个动作动画（webm）、气泡、聊天面板和 Electron 运行时。仓库里原先写的 clawd-on-desk 并不是实际在跑的那个壳。

## 安装

前置：已经装好并至少启动过一次 DeepSeek Harness（DSH），存在 `%USERPROFILE%\.dsh\profiles\web`。

> **DSH 目前未公开发布，没有官方下载地址，本仓库也不分发。** 没有 DSH 的机器暂时无法用这种方式装桌宠。

```powershell
powershell -ExecutionPolicy Bypass -File .\pet\dsh-pet\install.ps1
```

脚本做三件事：

1. `npm install ./pet/dsh-pet/dsh-pet-0.4.1.tgz`（装进 `%USERPROFILE%\.dsh\profiles\web`，版本锁定 0.4.1）
2. `npm run ensure:electron`（下载 Electron 运行时）
3. 把 `dsh-pet` 写进该 profile 的 `dsh.profile.bundles`

装完重启 DSH，在插件里启用 dsh-pet 即可。个人数据目录在 `%USERPROFILE%\.dsh\dsh-pet`（配置、记忆、动作缓存）。

## 说明

- 本目录里的 `dsh-pet-0.4.1.tgz` 就是从本机 DSH 安装目录里取出的同款安装包（62.4 MB，1077 个文件），保证别人下载后跑出来和本机一致。
- dsh-pet 自身以上游 MIT 许可发布（作者 PC2005-cloud，npm 包名 `dsh-pet`，首页 https://github.com/PC2005-cloud/dsh-pet ），二次分发请保留其 LICENSE。
- 首次安装时若 `%USERPROFILE%\.dsh\dsh-pet\main-config.jsonc` 不存在，脚本会自动铺入本目录的 `config.example.jsonc`（碎碎念人设、海草+背景泡泡、睡觉台词等，带中文注释，可自行修改）。
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
