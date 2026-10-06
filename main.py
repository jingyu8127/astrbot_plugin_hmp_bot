"""
HMP Bot —— HaulMP 平台查询插件

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
"""

import os
import re
import json
import math
import sys
import asyncio
import uuid
import unicodedata
import aiohttp

# 确保插件自身目录在 sys.path 上，兼容不同插件加载方式下的 `import map_render`
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import map_render

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

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

# 单监听器 + 正则路由：兼容带 / 或不带 / 的写法，且能优雅地处理
# “查询”（无参数）与“查询 xxx”（带参数）等情况。
RE_QUERY = re.compile(r"^(?:/)?查询\s*(.*)$")
RE_BIND = re.compile(r"^(?:/)?绑定\s*(.*)$")
RE_MY = re.compile(r"^(?:/)?我的绑定\s*$")
RE_UNBIND = re.compile(r"^(?:/)?解绑\s*(.*)$")
RE_SERVER = re.compile(r"^(?:/)?服务器\s*$")
RE_LOCATE = re.compile(r"^(?:/)?定位\s*(.*)$")
RE_TRAFFIC = re.compile(r"^(?:/)?路况\s*$")

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


def heading_to_bearing(x, z, h):
    """游戏朝向 h -> 屏幕方位角（度，0=正北，顺时针）。

    复刻官网 lM()：把 (x - sin h, z - cos h) 与 (x, z) 都投影到经纬度，
    再在墨卡托空间里求方位角，从而抵消 LCC→墨卡托的形变。
    """
    lon1, lat1 = game_to_lonlat(x, z)
    lon2, lat2 = game_to_lonlat(x - math.sin(h), z - math.cos(h))

    def _my(lat):
        return math.log(math.tan(math.pi / 4 + lat * math.pi / 360))

    return math.degrees(math.atan2((lon2 - lon1) * math.pi / 180,
                                   _my(lat2) - _my(lat1)))


def _haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _url_image(url):
    """把网络图片 URL 包装成图片消息组件（API 兼容同上）。"""
    from_url = getattr(getattr(Comp, "Image", None), "fromURL", None)
    if callable(from_url):
        return from_url(url)
    return Comp.Image(file=url)


def _local_image(path):
    """把本地图片路径包装成图片消息组件。

    不同 AstrBot 版本的 Image API 略有差异：优先使用官方推荐的
    Image.fromFileSystem()，该方法不存在时退回 Image(file=...)。
    """
    from_fs = getattr(getattr(Comp, "Image", None), "fromFileSystem", None)
    if callable(from_fs):
        return from_fs(path)
    return Comp.Image(file=path)


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

        # 后台预热海面/字体等静态资源（避免首次出图时再等网络）
        try:
            import threading
            threading.Thread(
                target=lambda: (map_render._load_water_b64(),
                                map_render._load_glyphs_b64()),
                daemon=True,
            ).start()
        except Exception:
            pass

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
                return [_url_image(avatar), Comp.Plain(text)]
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

    # ---------- 业务逻辑：定位 ----------
    @staticmethod
    def _find_player(players, handle):
        """按名字查找玩家：先精确、再子串；均忽略大小写与变音符号
        （复刻官网 Uo() 的 NFD 分解后去组合符处理，使 Jagermeister 能命中
        Jägermeister）。"""
        def _norm(s):
            s = unicodedata.normalize("NFD", str(s or ""))
            s = "".join(c for c in s if not unicodedata.combining(c))
            return s.lower().strip()
        h = _norm(handle)
        exact = next((p for p in players if _norm(p.get("name")) == h), None)
        if exact:
            return exact
        return next((p for p in players if h in _norm(p.get("name"))), None)

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

        # 附近 60km 内的其他玩家（用于文案统计）
        nearby = []
        for p in players:
            if p.get("id") == target.get("id"):
                continue
            try:
                plon, plat = game_to_lonlat(float(p.get("x") or 0), float(p.get("z") or 0))
            except Exception:
                continue
            d = _haversine_km(lon, lat, plon, plat)
            if d <= 60:
                nearby.append((d, p, plon, plat))
        nearby.sort(key=lambda t: t[0])

        # 定位用 z13（与官网「Follow player」Uu(mr(x,z),13) 完全一致）。
        # 注意：不要用 z14 瓦片过采样 —— 实测 z14 瓦片与游戏地图错位
        # （同一位置 z13 距路 11m、z14 却有 1198m），z13 才是准确数据源。
        locate_zoom = 13
        locate_tile_zoom = None
        # 宽幅画幅（比例接近官网视图）。竖幅会被聊天端按高度缩放，导致两侧留白
        locate_w, locate_h = 1280, 900
        m_per_px = map_render.meters_per_px(lat, locate_zoom)
        # 取“半高”作为可见半径，保证标点在画幅内
        view_radius_km = (locate_h / 2) * m_per_px / 1000.0
        in_view = [t for t in nearby if t[0] <= view_radius_km * 1.05]

        def _brg(pp):
            """玩家朝向 -> 屏幕方位角度；无 h 时返回 None（画成圆点）。"""
            try:
                hv = pp.get("h")
                if hv is None:
                    return None
                return heading_to_bearing(float(pp["x"]), float(pp["z"]), float(hv))
            except Exception:
                return None

        points = [{
            "lon": lon, "lat": lat, "name": name, "kind": "target",
            "sub": f"{lat:.4f}°N,{lon:.4f}°E  {max(0, round(speed))}km/h",
            "bearing": _brg(target),
        }]
        for d, p, plon, plat in in_view[:30]:
            points.append({
                "lon": plon, "lat": plat, "name": p.get("name"),
                "kind": "near", "sub": f"{d:.0f}km", "bearing": _brg(p),
            })

        stats = [
            f"状态：{'🚚行驶' if moving else '🅿️停靠'}",
            f"坐标：{lat:.4f},{lon:.4f}",
        ]
        if ghost:
            stats.append("⚠️安全区")

        svg_path = os.path.join(self.maps_dir, f"locate_{uuid.uuid4().hex}.svg")

        def _render():
            svg = map_render.build_map_svg(
                f"HaulMP 实时定位 · {name}", (lon, lat), points,
                stats=stats, zoom=locate_zoom, tile_zoom=locate_tile_zoom,
                width=locate_w, height=locate_h,
            )
            if not svg:
                raise RuntimeError("SVG 渲染不可用（缺少 mapbox-vector-tile）")
            with open(svg_path, "w", encoding="utf-8") as fh:
                fh.write(svg)

        try:
            # 出图需抓取网络瓦片（可能数秒），放到线程执行以免阻塞事件循环
            await asyncio.to_thread(_render)
        except Exception as e:
            logger.warning("渲染定位地图失败: %s", e)
            return f"❌ 地图渲染失败：{e}"

        summary = (
            f"📍 {name} 实时定位：{lat:.4f}°N, {lon:.4f}°E，"
            f"{'行驶中' if moving else '停靠'}"
            + ("，处于安全区" if ghost else "")
            + f"，附近 60km 内 {len(nearby)} 人"
            + (f"（图中可见 {len(in_view)} 人）。" if in_view else "。")
        )
        return [Comp.Plain(summary), _local_image(svg_path)]

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
        for p in players:
            try:
                lon, lat = game_to_lonlat(float(p.get("x") or 0), float(p.get("z") or 0))
            except Exception:
                continue
            points.append({
                "lon": lon, "lat": lat, "name": p.get("name"),
                "kind": "ghost" if p.get("ghost") else "player",
            })
        if not points:
            return "当前地图暂无可定位的玩家。"
        # 按全体玩家范围自适应取景（画幅贴合内容，减少空白）
        try:
            center, zoom, map_w, map_h = map_render.fit_view(points)
        except Exception:
            center = (sum(p["lon"] for p in points) / len(points),
                      sum(p["lat"] for p in points) / len(points))
            zoom, map_w, map_h = 4, 1080, 1080

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

        svg_path = os.path.join(self.maps_dir, f"traffic_{uuid.uuid4().hex}.svg")

        def _render():
            svg = map_render.build_map_svg(
                "HaulMP 实时路况", center, points, stats=stats, zoom=zoom,
                width=map_w, height=map_h,
            )
            if not svg:
                raise RuntimeError("SVG 渲染不可用（缺少 mapbox-vector-tile）")
            with open(svg_path, "w", encoding="utf-8") as fh:
                fh.write(svg)

        try:
            # 出图需抓取网络瓦片（可能数秒），放到线程执行以免阻塞事件循环
            await asyncio.to_thread(_render)
        except Exception as e:
            logger.warning("渲染路况地图失败: %s", e)
            return f"❌ 地图渲染失败：{e}"

        summary = (
            f"🚦 HaulMP 实时路况：在线 {total}，行驶 {moving}，"
            f"停靠 {total - moving}，安全区 {ghost}。"
        )
        return [Comp.Plain(summary), _local_image(svg_path)]

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

    async def terminate(self):
        """插件被卸载/停用时会调用，可做资源清理。"""
        logger.info("HMP Bot 已停止。")
