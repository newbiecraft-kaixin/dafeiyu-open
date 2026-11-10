# 贡献指南

感谢你对大肥鱼（Dafeiyu）的关注。提交代码前请先读一遍本文件。

## 开发环境

- Windows 10/11
- Python 3.11+
- Node.js 20+
- 第三方组件：AstrBot 4.x、NapCat（可选）、CosyVoice2（可选）、Ollama（可选）

## 提交流程

1. Fork 本仓库并从 `main` 切出功能分支：`feat/xxx`、`fix/xxx`、`docs/xxx`。
2. 只提交源码与配置模板，**不要提交**以下内容：
   - `.env`、真实 Token / Cookie / API Key
   - QQ 号、群号、wxid、手机号、邮箱等个人身份信息
   - `data/`、`logs/`、运行数据库、聊天记录、模型权重
3. 提交前至少做一次语法检查：
   - Python：`python -m compileall -q services astrbot_plugins shared tools`
   - Node：`node --check relay/pet-relay.js`
4. 在 PR 描述里写清：改动目的、影响范围、验证方式。

## 配置约定

- 所有可配置项优先放在各服务的 `config.json` / `service.json`，代码里只保留占位默认值。
- 敏感配置一律通过环境变量注入，命名使用 `大写_下划线`。
- 新增服务请提供 `service.json`（名称、端口、依赖、健康检查），便于 hub 自动发现。

## 代码风格

- Python 使用 4 空格缩进，保持现有类型标注风格。
- JavaScript 使用 2 空格缩进。
- 注释解释「为什么」，不要复述「做了什么」。

## 许可

提交即表示你同意以 AGPL-3.0 许可发布你的贡献。
