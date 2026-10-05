# astrbot_plugin_hmp_bot

基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件开发流程搭建的插件脚手架。

## 功能

- `/hmp`：示例指令，回复问候语。
- `/hmp_echo <文本>`：回显你发送的内容。
- `/hmp ping`：指令组示例。
- `/hmp_admin`：仅管理员可用。
- `/hmp_img <图片URL>`：发送一张图片。

> 这是脚手架，具体业务逻辑待实现。

## 目录结构

```
astrbot_plugin_hmp_bot/
  metadata.yaml      # 插件元数据（必需）
  main.py            # 插件代码主体（必需）
  _conf_schema.json  # 插件配置 Schema（可选，提供 WebUI 可视化配置）
  requirements.txt   # pip 依赖（可选）
  README.md
```

## 开发原则

- 持久化数据存放到 `data` 目录，避免更新被覆盖。
- 网络请求使用 `aiohttp` / `httpx`，禁止使用 `requests`。
- 日志使用 `from astrbot.api import logger`。
- 提交前使用 `ruff` 格式化代码。

## 调试

启动 AstrBot 本体后，将本插件放入 `AstrBot/data/plugins/` 目录，
在 WebUI「插件」页面对应卡片点击刷新图标即可热重载。
