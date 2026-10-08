
"""
HMP Bot —— HaulMP 平台查询插件（基于 AstrBot）。

功能与命令：
- 绑定 HaulMP 论坛用户名：  绑定 [用户名]        （每人最多 3 个，首个为主账号）
- 查看我的绑定：            我的绑定
- 解绑：                    解绑 [序号 / 用户名 / 全部]
- 查询玩家资料：
    未绑定：查询 [用户名]   （必须带上要查的用户名）
    已绑定：查询           （省略用户名，直接查主账号）
- 服务器状态：              服务器H            （在线人数 / 客户端版本）
- 实时定位：                定位 [用户名]        （未绑定必填；已绑定可省略直接定位主账号）
- 实时路况：                路况                （全量在线玩家分布 / 行驶停靠统计）
定位与路况均返回**地图图片**：先把游戏坐标经 LCC 投影换算为真实经纬度，再本地渲染
成地图（目标红色标注 + 附近玩家 / 全量玩家散布 + 经纬网格 + 比例尺）。
输出格式由配置项 output 控制：text（纯文本，默认）或 image（头像+文本图文卡片，仅作用于查询）。
"""

import os
import re
import json
import math
import asyncio
import uuid
import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

# 地图渲染统一使用 AstrBot 内置的 t2i 服务（html_renderer），不再自带 Node/Chromium。
_HERE = os.path.dirname(os.path.abspath(__file__))
_TPL_DIR = os.path.join(_HERE, "leaflet_templates")
_VENDOR_DIR = os.path.join(_TPL_DIR, "vendor")

def _read_vendor(rel: str) -> str:
    with open(os.path.join(_VENDOR_DIR, rel), "r", encoding="utf-8") as _f:
        return _f.read()

FORUM_API = "https://forum.haulmp.com/api/forum"
STATUS_URL = "https://haulmp.com/api/status"
BINDINGS_FILE = "haulmp_bindings.json"
MAX_BINDINGS = 3
FORUM_BASE = "https://forum.haulmp.com"
# 部分 HaulMP 接口会按 User-Agent 拦截（默认 Python / aiohttp UA 会被 403），统一带浏览器 UA。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


# 「搜人」筛选字段别名 -> (会员目录字段名, 类型)。目录对象不含 company/bio/links/driving 等
# 仅「查询」详情才有，见 _DETAIL_ONLY_FIELDS。类型：str 子串 / num 数值比较 / bool 布尔。
_SEARCH_FIELDS = {
    "用户名": ("handle", "str"), "名字": ("handle", "str"), "name": ("handle", "str"),
    "显示名": ("displayName", "str"), "显示名称": ("displayName", "str"),
    "用户id": ("id", "str"), "id": ("id", "str"),
    "角色": ("role", "str"),
    "管理员": ("admin", "bool"), "admin": ("admin", "bool"),
    "支持者": ("supporter", "num"), "支持者等级": ("supporter", "num"),
    "在线": ("online", "bool"), "在线状态": ("online", "bool"),
    "车队成员": ("vtcMember", "bool"), "vtc成员": ("vtcMember", "bool"),
    "里程": ("km", "num"), "总里程": ("km", "num"), "驾驶里程": ("km", "num"),
    "交付": ("deliveries", "num"), "交付次数": ("deliveries", "num"),
    "最长单程": ("longestKm", "num"),
    "活跃天数": ("activeDays", "num"),
    "帖子": ("posts", "num"), "帖子数": ("posts", "num"),
    "声望": ("reputation", "num"),
    "国家": ("country", "str"),
    "签名": ("signature", "str"),
    "注册时间": ("memberSince", "str"),
    "最后活跃": ("lastActive", "str"),
}
# 这些字段只在「查询」的详情里，目录接口没有，无法用于「搜人」筛选
_DETAIL_ONLY_FIELDS = {"网站", "个人网站", "简介", "bio", "车队名称", "车队标签", "驾驶时长", "时长"}
_FILTER_RE = re.compile(r"^(.+?)(>=|<=|!=|>|<|=)(.+)$")
_TAG_RE = re.compile(r"\[[^\]]*\]")   # 剥离游戏内名称里的 [车队] 标签前缀
_SEARCH_MAX_PAGES = 5          # 有筛选条件时最多扫描的目录页数（每页 40）

# 单监听器 + 正则路由：兼容带 / 或不带 / 的写法，且能优雅地处理
# “查询”（无参数）与“查询 xxx”（带参数）等情况。
RE_QUERY = re.compile(r"^(?:/)?查询\s*(.*)$")
RE_BIND = re.compile(r"^(?:/)?绑定\s*(.*)$")
RE_MY = re.compile(r"^(?:/)?我的绑定\s*$")
RE_UNBIND = re.compile(r"^(?:/)?解绑\s*(.*)$")
RE_SERVER = re.compile(r"^(?:/)?服务器\s*$")
RE_LOCATE = re.compile(r"^(?:/)?定位\s*(.*)$")
RE_TRAFFIC = re.compile(r"^(?:/)?路况\s*$")
RE_SEARCH = re.compile(r"^(?:/)?搜人\s*(.*)$")
RE_MENU = re.compile(r"^(?:/)?菜单\s*$")

# ---------- 实时地图投影（复刻 map 源码 src/projection.js 的球面 LCC） ----------
LIVE_URL = "https://map.haulmp.com/api/live"
_R = 6370997.0
_DEG = _R * math.pi / 180.0
_PHI1 = math.radians(37)
_PHI2 = math.radians(65)
_PHI0 = math.radians(50)
_LAM0 = math.radians(15)


def _lcc_inverse(X, Y):
    """LCC（球面）逆投影：投影坐标(米) -> (lon, lat) 度。"""
    n = math.log(math.cos(_PHI1) / math.cos(_PHI2)) / math.log(
        math.tan(math.pi / 4 + _PHI2 / 2) / math.tan(math.pi / 4 + _PHI1 / 2))
    F = math.cos(_PHI1) * (math.tan(math.pi / 4 + _PHI1 / 2) ** n) / n
    rho0 = _R * F / (math.tan(math.pi / 4 + _PHI0 / 2) ** n)
    rho = math.sqrt(X * X + (rho0 - Y) ** 2)
    theta = math.atan2(X, rho0 - Y)
    t = (_R * F / rho) ** (1.0 / n)
    phi = 2 * math.atan(t) - math.pi / 2
    return math.degrees(_LAM0 + theta / n), math.degrees(phi)


def game_to_lonlat(x, z):
    """游戏坐标 (x, z) -> 真实经纬度 (lon, lat)，复刻 coordinates(x, z)。"""
    sx, sz = math.floor(x / 4000), math.floor(z / 4000)
    east, north = x - 16660.0, z - 4150.0
    if sx <= -8 and sz <= -2 and not (sx == -8 and sz == -2):
        east, north = (east - 15550.0) * 0.75, (north - 2750.0) * 0.75
    X = east * 0.0001729241463 * _DEG
    Y = north * (-0.000171570875) * _DEG
    return _lcc_inverse(X, Y)


# 定位指令中“附近玩家”的搜索半径（公里）
_LOCATE_NEARBY_KM = 5.0


def _haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class HmpBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 由 _conf_schema.json 解析而来，继承自 dict

        # 持久化目录：data/plugins/<插件名>，不要写到插件自身目录
        self.data_dir = os.path.join("data", "plugins", "astrbot_plugin_hmp_bot")
        os.makedirs(self.data_dir, exist_ok=True)
        self.bindings_path = os.path.join(self.data_dir, BINDINGS_FILE)
        # 生成的定位/路况地图图片存放目录
        self.maps_dir = os.path.abspath(os.path.join(self.data_dir, "maps"))
        os.makedirs(self.maps_dir, exist_ok=True)


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
                    url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)
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
                    STATUS_URL, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except Exception as e:
            logger.warning("查询 HaulMP 服务器状态失败: %s", e)
            return None

    # ---------- 网络：查询 HaulMP 实时地图 ----------
    async def _fetch_live(self) -> dict | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    LIVE_URL, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except Exception as e:
            logger.warning("查询 HaulMP 实时地图失败: %s", e)
            return None

    # ---------- 格式化：玩家资料卡 ----------
    @staticmethod
    def _format_profile(member: dict) -> str:
        handle = member.get("handle") or "?"
        display = member.get("displayName") or handle
        # 在线状态优先反映「游戏内」是否在线（来自 map live 接口）；
        # 拉取失败时优雅降级为论坛平台在线状态。
        platform_online = bool(member.get("online"))
        in_game = member.get("inGame")
        if in_game is True:
            online = "🟢 游戏内在线"
        elif in_game is False:
            online = "⚪ 游戏内离线"
        else:
            online = f"{'🟢 在线' if platform_online else '⚪ 离线'}（游戏内未知）"
        admin = bool(member.get("admin"))
        role = member.get("role") or "成员"
        supporter = member.get("supporter") or 0
        company = member.get("company") or {}
        driving = member.get("driving") or {}
        links = member.get("links") or {}
        active_days = member.get("activeDays")
        website = links.get("website") or ""
        signature = (member.get("signature") or "").strip()
        bio = (member.get("bio") or "").strip()

        lines = [
            "📋 HaulMP 玩家资料",
            f"用户名：{handle}",
            f"显示名：{display}",
            f"用户ID：{member.get('id') or '—'}",
            f"在线状态：{online}",
            f"角色：{role}{'（管理员）' if admin else ''}",
            f"支持者等级：Lv{supporter}",
            f"注册时间：{(member.get('memberSince') or '')[:10] or '—'}",
            f"最后活跃：{(member.get('lastActive') or '')[:19] or '—'}",
            f"活跃天数：{active_days if active_days is not None else 0} 天",
            f"国家：{member.get('country') or '未知'}",
            f"头衔：{member.get('title') or '无'}",
            f"个人网站：{website or '无'}",
            f"签名：{signature or '无'}",
            f"简介：{bio or '无'}",
            "— 车队 —",
            f"车队成员：{'是' if member.get('vtcMember') else '否'}",
            f"车队名称：{company.get('name') or '无'}",
            f"车队标签：{company.get('tag') or '无'}",
            "— 驾驶指标 —",
            f"总驾驶里程：{(member.get('km') or 0):,} km",
            f"驾驶时长：{(driving.get('hours') or 0):,} 小时",
            f"完成交付：{(member.get('deliveries') or 0):,} 次",
            f"最长单程：{(member.get('longestKm') or 0):,} km",
            "— 论坛指标 —",
            f"帖子：{(member.get('posts') or 0):,}",
            f"声望：{member.get('reputation') or 0}",
            f"主题：{(member.get('threads') or 0):,}",
            f"解答：{member.get('solutions') or 0}",
            f"零罚交付：{member.get('cleanDeliveries') or 0}",
            f"夜间交付：{member.get('nightDeliveries') or 0}",
            f"车队活动：{member.get('convoys') or 0}",
        ]
        return "\n".join(lines)

    # ---------- 检索：会员目录筛选与格式化 ----------
    @staticmethod
    def _parse_filter(token: str):
        """解析单个筛选条件，返回 (字段, 操作符, 值, 类型)；无法解析返回 None。"""
        m = _FILTER_RE.match(token)
        if not m:
            key = _SEARCH_FIELDS.get(token)
            if key and key[1] == "bool":
                return (key[0], "=", True, "bool")
            return None
        key = _SEARCH_FIELDS.get(m.group(1)) or _SEARCH_FIELDS.get(m.group(1).lower())
        if not key:
            return None
        name, typ = key
        op = m.group(2)
        raw = m.group(3).strip()
        if typ == "bool":
            b = raw.lower() in ("1", "true", "yes", "y", "是", "真", "有", "yes")
            return (name, op, b, "bool")
        if typ == "num":
            try:
                return (name, op, float(raw), "num")
            except ValueError:
                return None
        return (name, op, raw, "str")

    @staticmethod
    def _is_detail_only(token: str):
        m = _FILTER_RE.match(token)
        name = (m.group(1) if m else token).strip()
        return name in _DETAIL_ONLY_FIELDS or name.lower() in _DETAIL_ONLY_FIELDS

    @staticmethod
    def _cond_pass(member: dict, name: str, op: str, val, typ: str) -> bool:
        if typ == "bool":
            v = bool(member.get(name))
            return (v == val) if op in ("=", "==") else (v != val)
        if typ == "num":
            try:
                v = float(member.get(name) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if op == ">":
                return v > val
            if op == "<":
                return v < val
            if op == ">=":
                return v >= val
            if op == "<=":
                return v <= val
            if op == "!=":
                return v != val
            return v == val
        v = str(member.get(name) or "").lower()
        s = str(val).lower()
        return (s in v) if op in ("=", "==") else (s not in v)

    @staticmethod
    def _format_member_line(i: int, m: dict) -> str:
        handle = m.get("handle") or "?"
        disp = m.get("displayName") or ""
        disp_s = f"（{disp}）" if disp and disp != handle else ""
        online = "🟢" if m.get("online") else "⚪"
        km = f"里程{(m.get('km') or 0):,}km"
        dvd = f"交付{(m.get('deliveries') or 0)}"
        role = m.get("role") or ""
        flags = []
        if m.get("admin"):
            flags.append("管理员")
        if m.get("vtcMember"):
            flags.append("车队")
        if m.get("supporter"):
            flags.append(f"Lv{m.get('supporter')}")
        return f"{i}. {online} {handle}{disp_s}  {km} {dvd} {role} {' '.join(flags)}".strip()

    # ---------- 网络：检索会员目录 ----------
    async def _fetch_members(self, keyword: str = "", page: int = 1) -> dict | None:
        params = [("page", str(page))]
        if keyword:
            params.append(("q", keyword))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{FORUM_API}/members", headers=_HEADERS, params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
        except Exception as e:
            logger.warning("检索 HaulMP 会员失败 q=%s: %s", keyword, e)
            return None

    # ---------- 业务逻辑：搜人（按字段检索/筛选） ----------
    async def _do_search(self, event: AstrMessageEvent, arg: str):
        arg = (arg or "").strip()
        tokens = arg.split()
        keyword = tokens[0] if tokens else ""
        raw_filters = tokens[1:]
        conds, hints = [], []
        for t in raw_filters:
            c = self._parse_filter(t)
            if c:
                conds.append(c)
            elif self._is_detail_only(t):
                hints.append(f"「{t}」仅在「查询」详情中，无法用于筛选")
            else:
                hints.append(f"无法识别筛选条件「{t}」")

        max_pages = _SEARCH_MAX_PAGES if conds else 1
        members, total, scanned = [], 0, 0
        for page in range(1, max_pages + 1):
            data = await self._fetch_members(keyword, page)
            if not data:
                break
            members.extend(data.get("members") or [])
            total = data.get("total", len(members))
            scanned += 1
            if data.get("pages", 1) <= page:
                break

        if not members:
            return f"未检索到「{keyword}」相关的 HaulMP 用户。"

        filtered = [m for m in members if all(self._cond_pass(m, *c) for c in conds)]
        shown = filtered[:15]
        header = f"🔎 检索「{keyword or '全部'}」共 {total} 人"
        if conds:
            header += f"，筛选后 {len(filtered)} 人"
        if scanned > 1:
            header += f"（仅扫描前 {scanned} 页）"
        lines = [header]
        if hints:
            lines.append("提示：" + "；".join(hints))
        if not shown:
            lines.append("（无符合条件的结果）")
        for i, m in enumerate(shown, 1):
            lines.append(self._format_member_line(i, m))
        if len(filtered) > 15:
            lines.append("… 仅显示前 15 条，可用「查询 <用户名>」查看完整资料")
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
        member, live = await asyncio.gather(
            self._fetch_profile(handle), self._fetch_live()
        )
        if not member:
            return (
                f"未找到 HaulMP 用户「{handle}」。\n"
                f"提示：可用「搜人 {handle}」按用户名/显示名模糊检索。"
            )
        # 游戏内在线状态：live 在线玩家列表中是否存在该玩家（按名称匹配）
        players = (live or {}).get("players") or []
        member["inGame"] = self._is_in_game(
            players, member.get("handle"), member.get("displayName")
        )
        return member  # 返回 member 字典，由监听器决定输出格式

    # ---------- 业务逻辑：服务器状态 ----------
    async def _do_server(self, event: AstrMessageEvent) -> str:
        data = await self._fetch_status()
        if not data:
            return "❌ 获取 HaulMP 服务器状态失败，请稍后重试。"
        return self._format_status(data)

    # ---------- 业务逻辑：定位 ----------
    @staticmethod
    def _find_player(players, handle):
        h = (handle or "").lower()
        exact = next((p for p in players if (p.get("name") or "").lower() == h), None)
        if exact:
            return exact
        return next((p for p in players if h in (p.get("name") or "").lower()), None)

    @staticmethod
    def _is_in_game(players, handle, display_name):
        """判断玩家当前是否「在游戏内」在线。

        map live 接口的 players 列表即当前在游戏内的玩家（名称常带 [车队] 标签）。
        以名称匹配：先剥离 [车队] 标签前缀，再与论坛 handle / displayName 做
        大小写不敏感精确比较；找不到玩家（空列表）返回 None 表示状态未知。
        """
        if not players:
            return None
        h = (handle or "").strip().lower()
        d = (display_name or "").strip().lower()
        for p in players:
            nm = (p.get("name") or "").strip()
            base = _TAG_RE.sub("", nm).strip().lower()
            low = nm.lower()
            if base == h or base == d or low == h or low == d:
                return True
        return False

    # ---------- 渲染：AstrBot 内置 t2i 服务（html_renderer） ----------
    async def _render_map(self, title, center, points, *, stats=None, out_path=None, mode="auto"):
        """定位/路况统一出口：渲染地图图片。

        若插件配置了 t2i_endpoint，直接 POST 到该端点（如本地 t2i_server.py），
        不依赖 AstrBot 是否提供 html_renderer；否则回退到 AstrBot 内置 t2i。
        若均未启用或渲染失败，调用方会回退到文字摘要输出。
        """
        if mode == "auto":
            mode = "locate" if any((p.get("kind") or "") == "target" for p in points) else "traffic"
        tpl_name = "locate.html" if mode == "locate" else "traffic.html"
        tpl_path = os.path.join(_TPL_DIR, tpl_name)
        data = {
            "title": title,
            "center": [center[0], center[1]],
            "points": [
                {
                    "lon": float(p["lon"]), "lat": float(p["lat"]),
                    "name": p.get("name") or "", "kind": p.get("kind", "player"),
                    "sub": p.get("sub") or "",
                }
                for p in points
            ],
            "stats": stats or [],
            "tileUrl": self.config.get("leaflet_tile_url") or "",
            "tileType": self.config.get("leaflet_tile_type") or "auto",
        }
        html = self._build_render_html(tpl_path, data)
        endpoint = (self.config.get("t2i_endpoint") or "").strip() or None

        # 优先使用用户配置的本地/自定义 t2i 端点，不依赖 AstrBot 是否启用 html_renderer
        if endpoint:
            url = endpoint.rstrip("/") + "/generate"
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    json={"html": html, "type": "jpeg", "quality": 90,
                          "width": 720, "height": 720, "full_page": True},
                    timeout=aiohttp.ClientTimeout(total=45),
                ) as r:
                    r.raise_for_status()
                    img = await r.read()
            if not out_path:
                out_path = os.path.join(self.maps_dir, f"{mode}_{uuid.uuid4().hex}.png")
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(img)
            return out_path

        renderer = getattr(self.context, "html_renderer", None)
        if renderer is None:
            raise RuntimeError("AstrBot 未提供 html_renderer（请确认已启用 t2i 服务，或在插件配置 t2i_endpoint 指向本地 t2i_server.py）")
        # return_url=False -> 返回本地图片路径，可直接用 Comp.Image(file=...) 发送
        return await renderer.render_custom_template(
            html, {}, return_url=False,
            options={"full_page": True, "type": "jpeg", "quality": 90},
        )

    def _build_render_html(self, tpl_path: str, data: dict) -> str:
        """读取本地 HTML 模板，内联 Leaflet 等前端资源（使模板对渲染端自包含），
        并以 {% raw %} 包裹避免 t2i 端 Jinja2 误解析脚本中的 {{ / {%。
        """
        with open(tpl_path, "r", encoding="utf-8") as f:
            html = f.read()
        css = _read_vendor("leaflet.min.css")
        js_leaflet = _read_vendor("leaflet.min.js")
        js_heat = _read_vendor("leaflet-heat.js")
        js_vg = _read_vendor("leaflet.vectorgrid.bundled.js")
        html = (
            html
            .replace('<link href="./vendor/leaflet.min.css" rel="stylesheet">', "<style>" + css + "</style>")
            .replace('<script src="./vendor/leaflet.min.js"></script>', "<script>" + js_leaflet + "</script>")
            .replace('<script src="./vendor/leaflet-heat.js"></script>', "<script>" + js_heat + "</script>")
            .replace('<script src="./vendor/leaflet.vectorgrid.bundled.js"></script>', "<script>" + js_vg + "</script>")
        )
        # 跳过 shiki 运行时注入（约 1.2MB），避免无谓膨胀
        hi = html.rindex("</head>")
        html = html[:hi] + '<script id="astrbot-t2i-shiki-runtime"></script>' + html[hi:]
        # 在 </body> 前注入数据并调用模板内已定义的 setData()
        data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\/")
        bi = html.rindex("</body>")
        html = html[:bi] + '<script>try{setData(' + data_json + ');}catch(e){console.error(e);}</script>' + html[bi:]
        # 整体以 {% raw %} 包裹，避免 t2i 端 Jinja2 把 JS 里的 {{ / {% 当作语法
        return "{% raw %}" + html + "{% endraw %}"


    async def _do_locate(self, event: AstrMessageEvent, handle: str):
        handle = handle.strip().lstrip("@")
        # 场景一：未绑定且未提供用户名 —— 提示用法
        if not handle:
            sender = event.get_sender_id()
            lst = self._load_bindings().get(sender, [])
            if lst:
                handle = lst[0]["handle"]  # 已绑定：直接定位主账号
            else:
                return (
                    "你还没有绑定(HaulMP)账号。\n"
                    "未绑定时请这样定位：定位 [用户名]\n"
                    "或先绑定：绑定 [用户名]"
                )

        # 场景二：已绑定（省略用户名）或显式提供用户名
        live = await self._fetch_live()
        if not live:
            return "❌ 获取实时地图数据失败，请稍后重试。"
        players = live.get("players") or []
        target = self._find_player(players, handle)
        if not target:
            return f"未找到玩家：{handle}"
        try:
            lon, lat = game_to_lonlat(float(target.get("x") or 0), float(target.get("z") or 0))
        except Exception:
            return f"玩家「{target.get('name')}」坐标异常，无法定位。"
        speed = float(target.get("speed") or 0)
        ghost = target.get("ghost")
        moving = speed > 0.5
        name = target.get("name")

        # 附近 N km 内的其他玩家（用于地图标点与文案）
        nearby = []
        for p in players:
            if p.get("id") == target.get("id"):
                continue
            try:
                plon, plat = game_to_lonlat(float(p.get("x") or 0), float(p.get("z") or 0))
            except Exception:
                continue
            d = _haversine_km(lon, lat, plon, plat)
            if d <= _LOCATE_NEARBY_KM:
                nearby.append((d, p, plon, plat))
        nearby.sort(key=lambda t: t[0])

        points = [{
            "lon": lon, "lat": lat, "name": name, "kind": "target",
            "sub": f"{lat:.4f}°N,{lon:.4f}°E  {max(0, round(speed))}km/h",
        }]
        for d, p, plon, plat in nearby[:15]:
            points.append({
                "lon": plon, "lat": plat, "name": p.get("name"),
                "kind": "near", "sub": f"{d:.0f}km",
            })

        stats = [
            f"状态：{'🚚行驶' if moving else '🅿️停靠'}",
            f"坐标：{lat:.4f},{lon:.4f}",
        ]
        if ghost:
            stats.append("⚠️安全区")

        try:
            out_path = await self._render_map(
                f"HaulMP 实时定位 · {name}", (lon, lat), points, stats=stats, mode="locate",
                out_path=os.path.join(self.maps_dir, f"locate_{uuid.uuid4().hex}.png"),
            )
        except Exception as e:
            logger.warning("渲染定位地图失败（改用文字输出）: %s", e)
            return self._locate_text(name, lon, lat, moving, ghost, nearby)

        summary = (
            f"📍 {name} 实时定位：{lat:.4f}°N, {lon:.4f}°E，"
            f"{'行驶中' if moving else '停靠'}"
            + ("，处于安全区" if ghost else "")
            + f"，附近 {_LOCATE_NEARBY_KM:.0f}km 内 {len(nearby)} 人。"
        )
        return [Comp.Plain(summary), Comp.Image(file=out_path)]

    def _locate_text(self, name, lon, lat, moving, ghost, nearby):
        """定位渲染失败时的文字摘要替代（不输出合成底图图片）。"""
        lines = [
            f"📍 {name} 实时定位（文字版）",
            f"状态：{'🚚 行驶' if moving else '🅿️ 停靠'}" + ("  ⚠️安全区" if ghost else ""),
            f"坐标：{lat:.4f}°N, {lon:.4f}°E",
            f"附近 {_LOCATE_NEARBY_KM:.0f}km 内：{len(nearby)} 人",
        ]
        if nearby:
            lines.append("— 附近玩家 —")
            for d, p, plon, plat in nearby[:15]:
                spd = max(0, round(float(p.get("speed") or 0)))
                lines.append(f"· {p.get('name')}  {d:.0f}km  {spd}km/h")
        return "\n".join(lines)

    # ---------- 业务逻辑：路况 ----------
    async def _do_traffic(self) -> list | str:
        live, status = await asyncio.gather(self._fetch_live(), self._fetch_status())
        if not live:
            return "❌ 获取实时地图数据失败，请稍后重试。"
        players = live.get("players") or []
        total = len(players)
        if total == 0:
            return "当前地图暂无在线玩家。"
        moving = sum(1 for p in players if float(p.get("speed") or 0) > 0.5)
        ghost = sum(1 for p in players if p.get("ghost"))

        points = []
        slon = slat = 0.0
        for p in players:
            try:
                lon, lat = game_to_lonlat(float(p.get("x") or 0), float(p.get("z") or 0))
            except Exception:
                continue
            points.append({
                "lon": lon, "lat": lat, "name": p.get("name"),
                "kind": "ghost" if p.get("ghost") else "player",
            })
            slon += lon
            slat += lat
        center = (slon / len(points), slat / len(points))

        stats = [
            f"地图在线：{total}",
            f"行驶：{moving}",
            f"停靠：{total - moving}",
            f"安全区：{ghost}",
        ]
        if status:
            online = status.get("online")
            stats.append(
                f"服务器：{'🟢' if online else '🔴'}"
                f"{status.get('players')}/{status.get('maxPlayers')}"
            )

        try:
            out_path = await self._render_map(
                "HaulMP 实时路况", center, points, stats=stats, mode="traffic",
                out_path=os.path.join(self.maps_dir, f"traffic_{uuid.uuid4().hex}.png"),
            )
        except Exception as e:
            logger.warning("渲染路况地图失败（改用文字输出）: %s", e)
            return self._traffic_text(players, total, moving, ghost, status)

        summary = (
            f"🚦 HaulMP 实时路况：在线 {total}，行驶 {moving}，"
            f"停靠 {total - moving}，安全区 {ghost}。"
        )
        return [Comp.Plain(summary), Comp.Image(file=out_path)]

    def _traffic_text(self, players, total, moving, ghost, status):
        """路况渲染失败时的文字摘要替代（不输出合成底图图片）。"""
        lines = [
            "🚦 HaulMP 实时路况（文字版）",
            f"地图在线：{total}",
            f"行驶：{moving}",
            f"停靠：{total - moving}",
            f"安全区：{ghost}",
        ]
        if status:
            online = status.get("online")
            lines.append(
                f"服务器：{'🟢' if online else '🔴'}"
                f"{status.get('players')}/{status.get('maxPlayers')}"
            )
        moving_list = [p for p in players if float(p.get("speed") or 0) > 0.5]
        moving_list.sort(key=lambda p: float(p.get("speed") or 0), reverse=True)
        if moving_list:
            lines.append("— 行驶中（最快） —")
            for p in moving_list[:10]:
                spd = max(0, round(float(p.get("speed") or 0)))
                lines.append(f"· {p.get('name')}  {spd}km/h")
        return "\n".join(lines)

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

        m = RE_SEARCH.match(text)
        if m:
            yield event.plain_result(await self._do_search(event, m.group(1)))
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

        m = RE_LOCATE.match(text)
        if m:
            res = await self._do_locate(event, m.group(1))
            if isinstance(res, str):
                yield event.plain_result(res)
            else:
                yield event.chain_result(res)
            return

        m = RE_TRAFFIC.match(text)
        if m:
            res = await self._do_traffic()
            if isinstance(res, str):
                yield event.plain_result(res)
            else:
                yield event.chain_result(res)
            return

        m = RE_MENU.match(text)
        if m:
            yield event.plain_result(self._menu_text())
            return

    # ---------- 菜单 ----------
    @staticmethod
    def _menu_text() -> str:
        return (
            "🚛 HMP Bot 指令菜单\n\n"
            "可用命令：\n"
            "1. 绑定 [用户名] —— 绑定 HaulMP 论坛账号（最多 3 个，首个为主账号）\n"
            "2. 我的绑定 —— 查看已绑定账号\n"
            "3. 解绑 [序号/用户名/全部] —— 解绑账号\n"
            "4. 查询 [用户名] —— 查询玩家资料（分组展示全部字段）\n"
            "5. 搜人 [关键字] [筛选条件] —— 按用户名/显示名检索用户\n"
            "6. 服务器 —— 查询服务器状态（在线人数/客户端版本）\n"
            "7. 定位 [用户名] —— 查询玩家实时位置（地图图片）\n"
            "8. 路况 —— 全量在线玩家路况（地图图片）\n\n"
            "提示：所有命令兼容带 / 或不带 / 的写法；绑定后查询/定位可省略用户名。"
        )

    # ---------- AstrBot 指令注册（桩方法：仅用于在菜单/行为列表中显示，实际逻辑在 on_message 路由） ----------
    @filter.command("菜单")
    async def cmd_menu(self, event: AstrMessageEvent):
        """显示 HMP Bot 指令菜单。"""
        return

    @filter.command("查询")
    async def cmd_query(self, event: AstrMessageEvent, handle: str | None = None):
        """查询 HaulMP 玩家资料（分组展示全部字段）。"""
        return

    @filter.command("搜人")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str | None = None):
        """按用户名/显示名检索用户，支持字段筛选。"""
        return

    @filter.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent, handle: str | None = None):
        """绑定 HaulMP 论坛账号（最多 3 个，首个为主账号）。"""
        return

    @filter.command("解绑")
    async def cmd_unbind(self, event: AstrMessageEvent, arg: str | None = None):
        """解绑 HaulMP 论坛账号（序号/用户名/全部）。"""
        return

    @filter.command("我的绑定")
    async def cmd_my(self, event: AstrMessageEvent):
        """查看已绑定的 HaulMP 账号列表。"""
        return

    @filter.command("服务器")
    async def cmd_server(self, event: AstrMessageEvent):
        """查询 HaulMP 服务器状态（在线人数/客户端版本）。"""
        return

    @filter.command("定位")
    async def cmd_locate(self, event: AstrMessageEvent, handle: str | None = None):
        """查询玩家实时位置（地图图片，标注附近玩家）。"""
        return

    @filter.command("路况")
    async def cmd_traffic(self, event: AstrMessageEvent):
        """全量在线玩家路况（地图图片 + 行驶/停靠统计）。"""
        return

    async def terminate(self):
        """插件被卸载/停用时会调用，可做资源清理。"""
        logger.info("HMP Bot 已停止。")

# 文件用途：插件主程序 —— AstrBot 指令注册与路由、HaulMP 接口调用、资料 / 定位 / 路况出图调度
