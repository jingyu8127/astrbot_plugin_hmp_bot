"""
HMP Bot —— HaulMP 平台查询插件（基于 AstrBot）。

开发要点（来自 AstrBot 官方插件开发文档）：
- 插件类必须继承自 astrbot.api.star.Star，且文件名必须为 main.py。
- 处理函数（Handler）必须写在插件类内部，前两个参数固定为 self 和 event。
- 回复消息用 `yield event.plain_result(...)`（生成器方式），多条内容用
  `yield event.chain_result([组件, ...])`。
- 日志请使用 astrbot.api.logger，不要用标准 logging 模块。
- 持久化数据存放到 data 目录，避免插件更新/重装时被覆盖。
- 网络请求使用 aiohttp 等异步库，禁止使用 requests。

功能与命令：
- 绑定 HaulMP 论坛用户名：  绑定 [用户名]        （每人最多 3 个，首个为主账号）
- 查看我的绑定：            我的绑定
- 解绑：                    解绑 [序号 / 用户名 / 全部]
- 查询玩家资料：
    未绑定：查询 [用户名]   （必须带上要查的用户名）
    已绑定：查询           （省略用户名，直接查主账号）
- 服务器状态：              服务器H            （在线人数 / 客户端版本）
输出格式由配置项 output 控制：text（纯文本，默认）或 image（头像+文本图文卡片）。
"""

import os
import re
import json
import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

FORUM_API = "https://forum.haulmp.com/api/forum"
STATUS_URL = "https://haulmp.com/api/status"
BINDINGS_FILE = "haulmp_bindings.json"
MAX_BINDINGS = 3
FORUM_BASE = "https://forum.haulmp.com"

# 单监听器 + 正则路由：兼容带 / 或不带 / 的写法，且能优雅地处理
# “查询”（无参数）与“查询 xxx”（带参数）等情况。
RE_QUERY = re.compile(r"^(?:/)?查询\s*(.*)$")
RE_BIND = re.compile(r"^(?:/)?绑定\s*(.*)$")
RE_MY = re.compile(r"^(?:/)?我的绑定\s*$")
RE_UNBIND = re.compile(r"^(?:/)?解绑\s*(.*)$")
RE_SERVER = re.compile(r"^(?:/)?服务器H\s*$")


class HmpBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 由 _conf_schema.json 解析而来，继承自 dict

        # 持久化目录：data/plugins/<插件名>，不要写到插件自身目录
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_hmp_bot")
        os.makedirs(self.data_dir, exist_ok=True)
        self.bindings_path = os.path.join(self.data_dir, BINDINGS_FILE)

    # ---------- 绑定存储（平台用户 -> [HaulMP 账号列表]） ----------
    def _load_bindings(self) -> dict:
        try:
            with open(self.bindings_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_bindings(self, data: dict) -> None:
        with open(self.bindings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- 网络：查询 HaulMP 论坛玩家资料 ----------
    async def _fetch_profile(self, handle: str) -> dict | None:
        """返回 member 字段；查不到 / 出错返回 None。"""
        url = f"{FORUM_API}/u/{handle}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            if isinstance(data, dict) and data.get("error"):
                return None
            return data.get("member")
        except Exception as e:  # 网络异常时优雅降级
            logger.warning("查询 HaulMP 资料失败 handle=%s: %s", handle, e)
            return None

    # ---------- 网络：查询 HaulMP 服务器状态 ----------
    async def _fetch_status(self) -> dict | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    STATUS_URL, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except Exception as e:
            logger.warning("查询 HaulMP 服务器状态失败: %s", e)
            return None

    # ---------- 格式化：玩家资料卡 ----------
    @staticmethod
    def _format_profile(member: dict) -> str:
        handle = member.get("handle") or "?"
        display = member.get("displayName") or handle
        status = "🟢 在线" if member.get("online") else "⚪ 离线"
        km = member.get("km") or 0
        company = member.get("company") or {}
        if company.get("name"):
            team = f"[{company.get('tag', '')}] {company.get('name')}"
        else:
            team = "未加入车队"
        member_since = (member.get("memberSince") or "")[:10]
        country = member.get("country") or "未知"
        title = member.get("title") or "无"
        deliveries = member.get("deliveries") or 0
        driving = member.get("driving") or {}
        hours = driving.get("hours") or 0

        lines = [
            "📋 HaulMP 玩家资料",
            f"用户名：{handle}",
            f"显示名称：{display}",
            f"在线状态：{status}",
            f"历史里程：{km:,} km",
            f"所属车队：{team}",
            f"国家：{country}",
            f"头衔：{title}",
            f"注册时间：{member_since}",
            f"总交付：{deliveries:,} 次",
            f"驾驶时长：{hours:,} 小时",
        ]
        return "\n".join(lines)

    # ---------- 格式化：服务器状态 ----------
    @staticmethod
    def _format_status(data: dict) -> str:
        online = data.get("online")
        players = data.get("players")
        max_players = data.get("maxPlayers")
        protocol = data.get("protocol")
        client = data.get("client") or {}
        version = client.get("version")
        notes = client.get("notes") or ""
        published = (client.get("publishedAt") or "")[:10]

        lines = [
            "🖥️ HaulMP 服务器状态",
            f"运行状态：{'🟢 在线' if online else '🔴 离线'}",
            f"在线人数：{players} / {max_players}",
            f"协议版本：{protocol}",
            f"客户端版本：{version}",
            f"版本说明：{notes}",
            f"发布时间：{published}",
        ]
        return "\n".join(lines)

    # ---------- 输出：纯文本 / 图文卡片 ----------
    def _build_profile_output(self, member: dict):
        """根据配置返回字符串（纯文本）或组件列表（图文卡片）。"""
        text = self._format_profile(member)
        if (self.config.get("output", "text") or "text").lower() == "image":
            avatar = member.get("avatar")
            if avatar:
                if avatar.startswith("/"):
                    avatar = FORUM_BASE + avatar
                return [Comp.Image.fromURL(avatar), Comp.Plain(text)]
        return text

    # ---------- 业务逻辑：绑定 ----------
    async def _do_bind(self, event: AstrMessageEvent, handle: str) -> str:
        handle = handle.strip().lstrip("@")
        if not handle:
            return "用法：绑定 [HaulMP 论坛用户名]\n例如：绑定 johndoe"
        member = await self._fetch_profile(handle)
        if not member:
            return f"未找到 HaulMP 用户「{handle}」，请确认用户名是否正确。"

        sender = event.get_sender_id()
        bindings = self._load_bindings()
        lst = bindings.get(sender, [])
        norm = member.get("handle", "").lower()
        if any(b["handle"].lower() == norm for b in lst):
            return (
                f"你已绑定过 HaulMP 账号「{member.get('handle')}」，"
                f"无需重复绑定。"
            )
        if len(lst) >= MAX_BINDINGS:
            return f"每个账号最多绑定 {MAX_BINDINGS} 个 HaulMP 账号。"

        lst.append(
            {
                "handle": member.get("handle"),
                "display_name": member.get("displayName"),
            }
        )
        bindings[sender] = lst
        self._save_bindings(bindings)

        if len(lst) == 1:
            return (
                f"✅ 已绑定 HaulMP 账号：{member.get('handle')}（"
                f"{member.get('displayName') or ''}），已设为默认查询账号。"
            )
        return (
            f"✅ 已绑定 HaulMP 账号：{member.get('handle')}（"
            f"{member.get('displayName') or ''}），当前共绑定 {len(lst)} 个。"
        )

    # ---------- 业务逻辑：我的绑定 ----------
    def _do_my_bindings(self, event: AstrMessageEvent) -> str:
        sender = event.get_sender_id()
        lst = self._load_bindings().get(sender, [])
        if not lst:
            return "你还没有绑定任何 HaulMP 账号。\n绑定方法：绑定 [用户名]"
        lines = ["📒 我的 HaulMP 绑定："]
        for i, b in enumerate(lst):
            tag = "（主账号）" if i == 0 else ""
            lines.append(f"{i + 1}. {b['handle']}（{b['display_name'] or ''}）{tag}")
        lines.append(f"\n共 {len(lst)} 个，最多 {MAX_BINDINGS} 个。")
        lines.append("查询默认查主账号；解绑：解绑 [序号 / 用户名 / 全部]")
        return "\n".join(lines)

    # ---------- 业务逻辑：解绑 ----------
    async def _do_unbind(self, event: AstrMessageEvent, target: str) -> str:
        target = target.strip().lstrip("@")
        sender = event.get_sender_id()
        bindings = self._load_bindings()
        lst = bindings.get(sender, [])
        if not lst:
            return "你还没有绑定任何 HaulMP 账号。"

        if target in ("", "全部", "all", "All", "ALL"):
            bindings[sender] = []
            self._save_bindings(bindings)
            return "✅ 已解除全部绑定。"

        # 按序号解绑
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(lst):
                removed = lst.pop(idx)
                bindings[sender] = lst
                self._save_bindings(bindings)
                return (
                    f"✅ 已解绑：{removed['handle']}（"
                    f"{removed['display_name'] or ''}）"
                )
            return f"序号 {target} 不存在，当前共 {len(lst)} 个绑定。"

        # 按用户名解绑
        norm = target.lower()
        for i, b in enumerate(lst):
            if b["handle"].lower() == norm:
                removed = lst.pop(i)
                bindings[sender] = lst
                self._save_bindings(bindings)
                return (
                    f"✅ 已解绑：{removed['handle']}（"
                    f"{removed['display_name'] or ''}）"
                )
        return f"未找到绑定「{target}」。先用「我的绑定」查看序号与用户名。"

    # ---------- 业务逻辑：查询 ----------
    async def _do_query(self, event: AstrMessageEvent, handle: str):
        handle = handle.strip().lstrip("@")

        # 场景一：未绑定且未提供用户名 —— 提示用法
        if not handle:
            sender = event.get_sender_id()
            lst = self._load_bindings().get(sender, [])
            if lst:
                handle = lst[0]["handle"]  # 已绑定：查主账号
            else:
                return (
                    "你还没有绑定 HaulMP 账号。\n"
                    "未绑定时请这样查询：查询 [用户名]\n"
                    "或先绑定：绑定 [用户名]"
                )

        # 场景二：已绑定（省略用户名）或未绑定时显式提供了用户名
        member = await self._fetch_profile(handle)
        if not member:
            return f"未找到 HaulMP 用户「{handle}」，请确认用户名是否正确。"
        return member  # 返回 member 字典，由监听器决定输出格式

    # ---------- 业务逻辑：服务器状态 ----------
    async def _do_server(self, event: AstrMessageEvent) -> str:
        data = await self._fetch_status()
        if not data:
            return "❌ 获取 HaulMP 服务器状态失败，请稍后重试。"
        return self._format_status(data)

    # ---------- 事件监听（接收所有消息，正则路由） ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()

        m = RE_QUERY.match(text)
        if m:
            res = await self._do_query(event, m.group(1))
            if isinstance(res, str):
                yield event.plain_result(res)
            else:
                out = self._build_profile_output(res)
                if isinstance(out, str):
                    yield event.plain_result(out)
                else:
                    yield event.chain_result(out)
            return

        m = RE_BIND.match(text)
        if m:
            yield event.plain_result(await self._do_bind(event, m.group(1)))
            return

        m = RE_MY.match(text)
        if m:
            yield event.plain_result(self._do_my_bindings(event))
            return

        m = RE_UNBIND.match(text)
        if m:
            yield event.plain_result(await self._do_unbind(event, m.group(1)))
            return

        m = RE_SERVER.match(text)
        if m:
            yield event.plain_result(await self._do_server(event))
            return

    async def terminate(self):
        """插件被卸载/停用时会调用，可做资源清理。"""
        logger.info("HMP Bot 已停止。")
