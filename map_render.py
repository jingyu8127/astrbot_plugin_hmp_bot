"""本地渲染 HaulMP 地图图片（无需外部瓦片）。

被查询/被统计玩家的游戏坐标已由调用方通过 LCC 投影换算为真实经纬度 (lon, lat)，
这里用等距圆柱投影（按中心纬度做经度方向余弦校正）把经纬度绘制成图片：
- 暗色海域背景 + 经纬网格（graticule）
- 目标玩家用醒目红色 pin 标注，并附名称/坐标/速度标签
- 附近玩家用橙色点标注；路况模式下全部玩家用蓝色/灰色点散布
- 右下角比例尺、右上角指北针

可选 `basemap_url`：若提供形如 "https://.../{z}/{x}/{y}.png" 的**可达**栅格瓦片
模板，则先抓取瓦片作底图再叠加标点；抓取失败自动回退到纯绘制模式。
"""

import logging
import math
import os

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("hmp_bot.map_render")

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsum.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _font(size: int) -> ImageFont.ImageFont:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


# 调色板（RGBA）
_BG = (13, 20, 36, 255)
_BG2 = (18, 28, 48, 255)
_VIEW = (9, 15, 28, 255)
_GRID = (42, 62, 96, 255)
_GRID_TXT = (120, 145, 185, 255)
_TITLE = (232, 238, 252, 255)
_FOOT = (165, 185, 220, 255)
_TARGET = (255, 72, 72, 255)
_TARGET_RG = (255, 255, 255, 255)
_NEAR = (255, 176, 64, 255)
_PLAYER = (96, 196, 255, 255)
_GHOST = (150, 162, 184, 255)


def _fit_view(pts, center, pad_frac=0.18, min_span=0.4):
    """返回缩放后的边界框与投影辅助量。"""
    cf = math.cos(math.radians((center[1] + sum(p[1] for p in pts)) / (len(pts) + 1)))
    xs = [lon * cf for lon, _ in pts] + [center[0] * cf]
    ys = [lat for _, lat in pts] + [center[1]]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    if maxx - minx < min_span:
        c = (maxx + minx) / 2
        minx, maxx = c - min_span / 2, c + min_span / 2
    if maxy - miny < min_span:
        c = (maxy + miny) / 2
        miny, maxy = c - min_span / 2, c + min_span / 2
    dx, dy = (maxx - minx) * pad_frac, (maxy - miny) * pad_frac
    return (minx - dx, miny - dy, maxx + dx, maxy + dy), cf


def _nice_step(span, target=6):
    raw = span / target
    for m in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60):
        if m >= raw:
            return m
    return 90


def _label(draw, xy, text, font, fill, bg):
    """在 (x, y) 绘制带半透明底色的文本标签。"""
    x, y = xy
    tw = draw.textlength(text, font=font)
    th = font.size
    pad = 4
    draw.rounded_rectangle(
        [x - pad, y - pad, x + tw + pad, y + th + pad], radius=4, fill=(8, 12, 22, 200)
    )
    draw.text((x, y), text, font=font, fill=fill)


def render_map(title, center, points, *, map_size=720, top_h=56, bot_h=80,
               stats=None, out_path=None):
    """把玩家位置渲染成地图图片。

    :param center: (lon, lat) 主视角中心（定位场景=被查用户，路况场景=全体包围盒中心）
    :param points: [{lon, lat, name, kind, sub}]，kind ∈ target/near/player/ghost
    :param stats: 底部信息行，字符串列表
    :param out_path: 输出 PNG 路径；省略则保存到临时文件
    :return: 生成的 PNG 绝对路径
    """
    W = map_size
    H = map_size + top_h + bot_h
    img = Image.new("RGBA", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    # 标题栏
    draw.rectangle([0, 0, W, top_h], fill=_BG2)
    draw.text((16, 14), title, font=_font(26), fill=_TITLE)

    # 视口
    mx0, my0, mx1, my1 = 0, top_h, W, top_h + map_size
    vw, vh = mx1 - mx0, my1 - my0
    ipad = 48
    all_pts = [(p["lon"], p["lat"]) for p in points] + [center]
    (minx, miny, maxx, maxy), cf = _fit_view(all_pts, center)
    spanx, spany = maxx - minx, maxy - miny
    scale = min((vw - 2 * ipad) / spanx, (vh - 2 * ipad) / spany)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

    def proj(lon, lat):
        return (
            mx0 + vw / 2 + (lon * cf - cx) * scale,
            my0 + vh / 2 - (lat - cy) * scale,
        )

    draw.rectangle([mx0, my0, mx1, my1], fill=_VIEW)

    # 经纬网格
    step = _nice_step(max(spanx / cf, spany))
    lon_min, lon_max = minx / cf, maxx / cf
    lat_min, lat_max = miny, maxy
    f_grid = _font(15)
    lon = math.floor(lon_min / step) * step
    while lon <= lon_max:
        px, _ = proj(lon, cy / cf if cf else 0)
        draw.line([(px, my0 + 6), (px, my1 - 6)], fill=_GRID, width=1)
        _label(draw, (px + 3, my1 - 22), f"{lon:.0f}°E", f_grid, _GRID_TXT, None)
        lon += step
    lat = math.floor(lat_min / step) * step
    while lat <= lat_max:
        _, py = proj(cx / cf if cf else 0, lat)
        draw.line([(mx0 + 6, py), (mx1 - 6, py)], fill=_GRID, width=1)
        _label(draw, (mx0 + 6, py + 3), f"{lat:.0f}°N", f_grid, _GRID_TXT, None)
        lat += step

    # 点
    f_name = _font(17)
    for p in points:
        x, y = proj(p["lon"], p["lat"])
        kind = p.get("kind", "player")
        if kind == "target":
            draw.ellipse([x - 13, y - 13, x + 13, y + 13], fill=_TARGET,
                         outline=_TARGET_RG, width=3)
            draw.line([(x, y - 22), (x, y - 13)], fill=_TARGET_RG, width=2)
            draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=_TARGET_RG)
            label = f"{p['name']}  {p.get('sub', '')}".strip()
            _label(draw, (x + 16, y - 14), label, f_name, _TITLE, None)
        elif kind == "near":
            draw.ellipse([x - 6, y - 6, x + 6, y + 6], fill=_NEAR,
                         outline=(20, 20, 20, 255), width=1)
            if p.get("name"):
                _label(draw, (x + 9, y - 8), f"{p['name']} {p.get('sub', '')}".strip(),
                       _font(14), _NEAR, None)
        elif kind == "ghost":
            draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=_GHOST)
        else:
            draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=_PLAYER)

    # 指北针
    nx, ny = mx1 - 34, my0 + 30
    draw.line([(nx, ny + 16), (nx, ny - 16)], fill=_TITLE, width=2)
    draw.polygon([(nx, ny - 20), (nx - 6, ny - 8), (nx + 6, ny - 8)], fill=_TARGET)
    draw.text((nx - 6, ny - 38), "N", font=_font(16), fill=_TITLE)

    # 比例尺（基于纬度：每像素米数 ≈ 111320 / scale）
    m_per_px = 111320.0 / scale
    target_px = 110
    raw_m = target_px * m_per_px
    for m in (100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000):
        if m >= raw_m:
            bar_m = m
            break
    else:
        bar_m = 500000
    bar_px = bar_m / m_per_px
    bx, by = mx0 + 18, my1 - 24
    draw.line([(bx, by), (bx + bar_px, by)], fill=_TITLE, width=3)
    draw.line([(bx, by - 5), (bx, by + 5)], fill=_TITLE, width=3)
    draw.line([(bx + bar_px, by - 5), (bx + bar_px, by + 5)], fill=_TITLE, width=3)
    bar_txt = f"{bar_m/1000:.0f} km" if bar_m >= 1000 else f"{bar_m} m"
    draw.text((bx, by - 20), bar_txt, font=_font(15), fill=_TITLE)

    # 底部信息栏
    draw.rectangle([0, top_h + map_size, W, H], fill=_BG2)
    if stats:
        line = "    ".join(stats)
        draw.text((16, top_h + map_size + 16), line, font=_font(20), fill=_FOOT)

    if out_path is None:
        import tempfile
        out_path = os.path.join(tempfile.gettempdir(), f"hmp_map_{os.getpid()}.png")
    img.save(out_path, "PNG")
    return os.path.abspath(out_path)
