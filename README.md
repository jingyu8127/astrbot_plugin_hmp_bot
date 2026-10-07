# astrbot_plugin_hmp_bot
[![version](https://img.shields.io/badge/version-v1.2.3-blue)](https://github.com/jingyu8127/astrbot_plugin_hmp_bot)



HaulMP（卡车模拟联机）查询机器人，用于查询玩家资料、检索用户、服务器状态、实时定位及全量路况等。

> [!NOTE]
> 「定位」「路况」为图片输出，由 **Leaflet + Puppeteer** 渲染（真实 ETS2 路网矢量瓦片底图 + 标记 / 路况热力图），需系统安装 `node` 并在插件目录执行 `npm install`（会下载 Chromium）。底图缺失时自动回退为合成暗色画布，仍可出图。

### 指令
| 指令 | 功能 | 示例 |
|------|------|------|
| 绑定 | 绑定 HaulMP 论坛账号（每人最多 3 个，首个为主账号），绑定后其他指令可省略用户名 | 绑定 jingyu |
| 我的绑定 | 查看已绑定的账号列表 | 我的绑定 |
| 解绑 | 解绑账号（按序号 / 用户名 / 全部） | 解绑 1 |
| 查询 | 查询玩家资料（分组展示全部字段） | 查询 jingyu |
| 搜人 | 按用户名/显示名检索用户，支持字段筛选 | 搜人 jing 里程>10000 |
| 服务器 | 查询服务器状态（在线人数 / 客户端版本） | 服务器 |
| 定位 | 查询玩家实时位置（地图图片，标注附近玩家） | 定位 jingyu |
| 路况 | 全量在线玩家路况（地图图片 + 行驶/停靠统计） | 路况 |

> 所有指令均兼容带 `/` 或不带 `/` 的写法（如 `/查询 jingyu`）。

### 搜人筛选条件
`搜人 [关键字] [筛选条件 ...]`，按**用户名/显示名**模糊检索（不分大小写），筛选条件可叠加：

| 类型 | 写法 | 示例 |
|------|------|------|
| 数值 | `字段>值` / `字段>=值` / `字段<值` / `字段<=值` | `里程>10000`、`交付>=10`、`支持者>=2` |
| 布尔 | `字段`（=是）或 `字段=是/否` | `管理员`、`车队成员=是`、`在线=否` |
| 文本 | `字段=值`（子串匹配） | `角色=管理员`、`国家=德国` |

支持的筛选字段：用户名、显示名、用户ID、角色、管理员、支持者、在线、车队成员、里程、交付、最长单程、活跃天数、帖子、声望、国家、签名、注册时间、最后活跃。

> [!WARNING]
> 车队名称 / 车队标签 / 简介 / 个人网站 / 驾驶时长仅在「查询」详情中展示，无法用于「搜人」筛选。

> [!NOTE]
> 「查询」资料卡中的**在线状态**反映的是**游戏内**是否在线（取自实时地图 `map.haulmp.com/api/live` 的在线玩家列表）；若实时地图暂时不可用，则降级显示论坛平台在线状态并标注「（游戏内未知）」。

### 配置项
| 配置项 | 说明 |
|--------|------|
| `output` | 资料查询输出格式：`text`（纯文本，默认）或 `image`（头像 + 图文卡片） |
| `leaflet_tile_url` | Leaflet 底图瓦片地址模板，留空使用 HaulMP 官方矢量瓦片 `map.haulmp.com/tiles/{z}/{x}/{y}.pbf` 真实路网 |
| `leaflet_tile_type` | 底图类型：`auto` / `raster` / `vector` / `none`（none 时合成暗色画布 + 经纬网格） |

### 接口与数据
数据来源：

- 玩家资料 / 会员目录：`https://forum.haulmp.com/api/forum`
- 服务器状态：`https://haulmp.com/api/status`
- 实时地图（定位 / 路况）：`https://map.haulmp.com/api/live`
- 地图矢量瓦片：`https://map.haulmp.com/tiles`

## 安装方法

直接在 AstrBot 插件市场搜索 `astrbot_plugin_hmp_bot` 点击安装；或复制仓库链接，到 AstrBot WebUI 插件页使用「链接安装」：

```
https://github.com/jingyu8127/astrbot_plugin_hmp_bot
```

### 安装地图渲染依赖（必需）

地图渲染依赖 **Leaflet + Puppeteer**（需系统安装 `node` 并下载 Chromium）：

1. 确认系统 PATH 中有 `node`（建议 v18+，本机已验证 v22）。
2. 进入插件目录（AstrBot 安装后的 `data/plugins/astrbot_plugin_hmp_bot/` 或源码目录）：
   ```bash
   npm install
   # 或仅安装渲染依赖
   npm install puppeteer
   ```
   Chromium 会被下载到 `~/.cache/puppeteer`（约 170MB）。
3. 可选：修改 `leaflet_tile_url` / `leaflet_tile_type`。
   - 留空：使用 HaulMP 官方矢量瓦片，显示真实 ETS2 路网。
   - 填 `none`：关闭底图，仅用合成暗色画布 + 经纬网格（纯离线、最快）。
   - 也可填自定义栅格地址：`https://.../{z}/{x}/{y}.png`。

> 若未安装 node / Chromium，`定位`、`路况` 会提示渲染失败。底图瓦片加载失败时则自动回退为合成暗色画布，仍可出图。

## 历史更新

## 版本 v1.2.3
- 移除 Pillow 本地渲染，`定位`/`路况` 完全改用 **Leaflet + Puppeteer** 渲染后端
- 删除失效配置项 `map_renderer`、`offline_tiles_dir`、`remote_tile_fallback`；移除 `requirements.txt` 中的 Pillow / mapbox-vector-tile
- 底图缺失时自动回退为合成暗色画布，仍可出图

## 版本 v1.2.2
- 新增 Leaflet + Puppeteer 地图渲染后端（可选），支持真实 ETS2 路网矢量瓦片底图与路况热力图
- 新增配置项 `map_renderer`、`leaflet_tile_url`、`leaflet_tile_type`
- Leaflet 渲染失败时自动回退 Pillow，默认仍为 Pillow

## 版本 v1.2.0
- 新增「搜人」命令：按用户名/显示名检索 + 字段筛选（数值 / 布尔 / 文本）
- 查询资料卡分组展示全部字段（基本信息 / 车队 / 驾驶指标 / 论坛指标）
- 地图渲染改用 Pillow 本地合成，支持离线瓦片目录与在线回退
- 服务器状态命令改为「服务器」

## 版本 v1.1.0
- 实时定位与全量路况地图（图片输出，标注附近玩家 + 行驶/停靠统计）
- 兼容 AstrBot Image API，出图改线程执行避免阻塞事件循环

## 版本 v1.0.0
- 初始版本：账号绑定、玩家资料查询、服务器状态

## 👥 贡献指南
- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

