# 桌宠（dsh-pet 0.4.1）

这台电脑上实际运行的桌宠就是 **dsh-pet**，它是 DeepSeek Harness（DSH）的桌宠插件，自带 109 个动作动画（webm）、气泡、聊天面板和 Electron 运行时。仓库里原先写的 clawd-on-desk 并不是实际在跑的那个壳。

## 安装

前置：已经装好并至少启动过一次 DeepSeek Harness（DSH），存在 `%USERPROFILE%\.dsh\profiles\web`。

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
- 与仓库里的 `pet/dafeiyu-web`（自研 Electron 页面）不是同一个东西：`dafeiyu-web` 是旧的网页版桌宠，dsh-pet 才是当前在桌面上跑的那只。
- 更新版本：`npm install dsh-pet@latest --save-exact` 装回 DSH profile 即可，不需要动本目录的 tgz（除非要同步仓库）。
