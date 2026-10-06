"""HaulMP 地图渲染
"""

import base64
import json
import logging
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote as _quote

logger = logging.getLogger("hmp_bot.map_render")

try:
    import mapbox_vector_tile as _mvt
    _HAVE_MVT = True
except Exception:  # pragma: no cover
    _HAVE_MVT = False

TILE_URL = "https://map.haulmp.com/tiles/{z}/{x}/{y}.pbf?v=160-roads-20260912"
WATER_URL = "https://map.haulmp.com/water.geojson"
MAPLIBRE_JS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js"
MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css"
_TILE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

GRID = 5                 # 预取瓦片网格边长（覆盖浏览器视口，留余量）
DEFAULT_ZOOM = 13
_MAX_TILE_ZOOM = 14            # 瓦片源 maxzoom（官网 style 里 game 源声明 maxzoom:14）
_TILE_CACHE = {}
_WATER = {"b64": None}


def _lonlat_to_frac(lon, lat, z):
    n = 1 << z
    fx = (lon + 180) / 360 * n
    latr = math.radians(lat)
    fy = (1 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2 * n
    return fx, fy


def _fetch_tile(z, x, y):
    """抓取一块矢量瓦片，返回原始 bytes（失败返回 None，不缓存失败）。"""
    key = (z, x, y)
    if key in _TILE_CACHE:
        return _TILE_CACHE[key]
    url = TILE_URL.format(z=z, x=x, y=y)
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _TILE_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            _TILE_CACHE[key] = raw
            return raw
        except Exception as e:  # 网络失败 -> 重试一次
            logger.warning("瓦片 %d/%d/%d 获取失败(%d/2): %s", z, x, y, attempt + 1, e)
    return None


_GLYPHS = {"data": None}
GLYPH_URL = "https://map.haulmp.com/fonts/{fontstack}/{range}.pbf"
_GLYPH_STACK = "Open Sans Regular"
_GLYPH_RANGES = ("0-255", "256-511")


def _load_glyphs_b64():
    """加载城市标注所需字形（base64），缓存。"""
    if _GLYPHS["data"] is None:
        out = {}
        for rng in _GLYPH_RANGES:
            try:
                url = GLYPH_URL.format(fontstack=_quote(_GLYPH_STACK), range=rng)
                req = urllib.request.Request(url, headers={"User-Agent": _TILE_UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    out[rng] = base64.b64encode(r.read()).decode()
            except Exception as e:
                logger.warning("字体 %s 加载失败: %s", rng, e)
        _GLYPHS["data"] = out
    return _GLYPHS["data"]


def _load_water_b64():
    """加载官方海面 GeoJSON（base64），缓存。"""
    if _WATER["b64"] is None:
        try:
            req = urllib.request.Request(WATER_URL, headers={"User-Agent": _TILE_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                _WATER["b64"] = base64.b64encode(r.read()).decode()
        except Exception as e:
            logger.warning("海面图层加载失败: %s", e)
            _WATER["b64"] = base64.b64encode(
                b'{"type":"FeatureCollection","features":[]}').decode()
    return _WATER["b64"]


def _tiles_b64(center, zoom, grid):
    """并发抓取 center 周边瓦片（grid 可为 N 或 (列, 行)），返回 {x/y/z: base64}。"""
    lon, lat = center
    fx, fy = _lonlat_to_frac(lon, lat, zoom)
    if isinstance(grid, (tuple, list)):
        gw, gh = int(grid[0]), int(grid[1])
    else:
        gw = gh = int(grid)
    x0 = int(math.floor(fx)) - gw // 2
    y0 = int(math.floor(fy)) - gh // 2
    n = 1 << zoom
    need = [(x0 + i, y0 + j) for i in range(gw) for j in range(gh)
            if 0 <= x0 + i < n and 0 <= y0 + j < n]

    def one(t):
        tx, ty = t
        raw = _fetch_tile(zoom, tx, ty)
        if raw is None:
            return None
        return ("%d/%d/%d" % (tx, ty, zoom), base64.b64encode(raw).decode())

    tiles = {}
    try:
        with ThreadPoolExecutor(max_workers=16) as ex:
            for r in ex.map(one, need):
                if r:
                    tiles[r[0]] = r[1]
    except Exception as e:
        logger.warning("瓦片预取异常: %s", e)
    return base64.b64encode(json.dumps(tiles).encode()).decode()


def _glyphs_b64():
    return base64.b64encode(json.dumps(_load_glyphs_b64()).encode()).decode()


_WORLD_M = 40075016.6855785   # Web Mercator 赤道周长（米）
_TILE_PX = 512                # MapLibre 矢量瓦片默认 512px


def meters_per_px(lat, zoom, tile=_TILE_PX):
    """MapLibre 下某纬度/缩放的每像素米数（瓦片 512px 约定）。"""
    return _WORLD_M / (tile * (2 ** zoom)) * math.cos(math.radians(lat))


def _yfrac(lat):
    latr = math.radians(lat)
    return (1 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2


def _fit_zoom(points, center, margin=1.4, size=1080):
    """根据点集包围盒估算能容纳所有点的缩放级别（MapLibre，size 像素视口）。"""
    lons = [p["lon"] for p in points] + [center[0]]
    lats = [p["lat"] for p in points] + [center[1]]
    span_lon = (max(lons) - min(lons)) / 360.0          # 世界宽度占比
    span_y = _yfrac(min(lats)) - _yfrac(max(lats))       # 世界高度占比（墨卡托）
    span = max(span_lon, span_y) * margin
    if span <= 0:
        return 12
    z = math.log(size / (span * _TILE_PX), 2)
    return max(2, min(14, int(math.floor(z))))


_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<link rel="stylesheet" href="__CSS__"/>
<script src="__JS__"></script>
<style>
  html,body{margin:0;padding:0;background:#141a22;font-family:"Microsoft YaHei",system-ui,Segoe UI,Helvetica,Arial,sans-serif;}
  #wrap{width:__W__px;}
  #hdr{height:56px;line-height:56px;padding:0 16px;background:#182034;color:#ecf0fa;font-size:24px;
       font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  #map{width:__W__px;height:__H__px;background:#f2efe8;}
  #ftr{padding:12px 16px;background:#182034;color:#afbcd6;font-size:15px;line-height:1.6;}
  .mk{position:relative;width:0;height:0;}
  .mk svg{position:absolute;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45));}
  .mk.pin svg{left:-14px;top:-34px;}
  .mk.dot svg{left:-6.5px;top:-6.5px;}
  .mk .lbl{position:absolute;left:7px;top:-15px;white-space:nowrap;font-size:15px;color:#fff;
           background:rgba(20,28,48,.86);padding:3px 8px;border-radius:5px;}
  .mk.orange .lbl{top:-10px;background:rgba(255,248,235,.92);color:#c25a00;}
</style></head>
<body><div id="wrap">
  <div id="hdr">__TITLE__</div>
  <div id="map"></div>
  <div id="ftr">__STATS__</div>
</div>
<script>
function b64buf(b){var s=atob(b),a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a.buffer;}
function b64txt(b){return decodeURIComponent(escape(atob(b)));}
var TILES=JSON.parse(b64txt("__TILES__"));
var WATER=JSON.parse(b64txt("__WATER__"));
var GLYPHS=JSON.parse(b64txt("__GLYPHS__"));
var DATA=JSON.parse(b64txt("__DATA__"));
maplibregl.addProtocol('hmp',function(p){
  var g=p.url.match(/^hmp:\\/\\/glyphs\\/[^\\/]+\\/(\\d+-\\d+)$/);
  if(g){ return g[1] in GLYPHS ? Promise.resolve({data:b64buf(GLYPHS[g[1]])}) : Promise.reject(new Error('no glyphs')); }
  var m=p.url.match(/^hmp:\\/\\/tiles\\/(\\d+)\\/(\\d+)\\/(\\d+)$/);
  if(!m) return Promise.reject(new Error('bad url'));
  var k=m[2]+'/'+m[3]+'/'+m[1];
  if(!(k in TILES)) return Promise.reject(new Error('tile missing '+k));
  return Promise.resolve({data:b64buf(TILES[k])});
});
var style={version:8,glyphs:"hmp://glyphs/{fontstack}/{range}",
  sources:{
    game:{type:'vector',tiles:['hmp://tiles/{z}/{x}/{y}'],minzoom:0,maxzoom:14},
    water:{type:'geojson',data:WATER}},
  layers:[
    {id:'bg',type:'background',paint:{'background-color':'#f2efe8'}},
    {id:'water',type:'fill',source:'water',paint:{'fill-color':'#a9c9de'}},
    {id:'prefabs',type:'fill',source:'game','source-layer':'ets2',filter:['==',['get','type'],'prefab'],
     paint:{'fill-color':'#dedcd4','fill-outline-color':'#c9c7bf'}},
    {id:'road-casing',type:'line',source:'game','source-layer':'ets2',filter:['==',['get','type'],'road'],
     layout:{'line-cap':'round','line-join':'round'},
     paint:{'line-color':'#b9a97e','line-width':['interpolate',['exponential',1.5],['zoom'],3,3.2,14,24,16,120]}},
    {id:'roads',type:'line',source:'game','source-layer':'ets2',filter:['==',['get','type'],'road'],
     layout:{'line-cap':'round','line-join':'round'},
     paint:{'line-color':['match',['get','roadType'],'freeway','#dfc071','divided','#c1ab73','#ae9e75'],
            'line-width':['interpolate',['exponential',1.5],['zoom'],3,1.6,14,20,16,109]}},
    {id:'cities',type:'symbol',source:'game','source-layer':'ets2',filter:['==',['get','type'],'city'],
     layout:{'text-field':['get','name'],'text-size':12,'text-offset':[0,0.5],'text-anchor':'top',
             'text-font':['Open Sans Regular']},
     paint:{'text-color':'#4a4a4a','text-halo-color':'#ffffff','text-halo-width':1.2}}
  ]};
var PIN='<svg width="28" height="34" viewBox="0 0 28 34"><path d="M14 0C7 0 1 6 1 13c0 10 13 21 13 21s13-11 13-21C27 6 21 0 14 0z" fill="#dc3232" stroke="#fff" stroke-width="2"/><circle cx="14" cy="13" r="5" fill="#fff"/></svg>';
function dotSvg(fill,stroke){return '<svg width="13" height="13" viewBox="0 0 13 13"><circle cx="6.5" cy="6.5" r="5" fill="'+fill+'" stroke="'+stroke+'" stroke-width="1.4"/></svg>';}
function mkEl(pt){
  var d=document.createElement('div'); var k=pt.kind;
  if(k==='target'){
    d.className='mk pin'; d.innerHTML=PIN+'<div class="lbl"></div>';
    d.querySelector('.lbl').textContent=pt.label; return d;
  }
  if(k==='near'){ d.className='mk dot orange'; d.innerHTML=dotSvg('#f0961e','#5a3c14'); }
  else if(k==='ghost'){ d.className='mk dot'; d.innerHTML=dotSvg('#9fb0c4','#3a4552'); }
  else { d.className='mk dot'; d.innerHTML=dotSvg('#3ca7ff','#12324a'); }
  if(k==='near' && pt.label){ var l=document.createElement('div'); l.className='lbl'; l.textContent=pt.label; d.appendChild(l); }
  return d;
}
try{
  var map=new maplibregl.Map({container:'map',style:style,center:DATA.center,zoom:DATA.zoom,
    attributionControl:false,interactive:false});
  DATA.points.forEach(function(pt){
    new maplibregl.Marker({element:mkEl(pt),anchor:'center'}).setLngLat([pt.lon,pt.lat]).addTo(map);
  });
  window.__map=map;
}catch(e){ document.getElementById('ftr').textContent='地图渲染异常: '+e.message; }
</script></body></html>"""


def _pct(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def fit_view(points, *, max_w=1100, max_h=1700, margin=1.10, min_zoom=2, max_zoom=14):
    """按点集范围挑选 (中心经纬, 缩放, 宽, 高)，画幅贴合内容以减少空白。"""
    lons = [p["lon"] for p in points]
    lats = [p["lat"] for p in points]
    lon0, lon1 = min(lons), max(lons)
    y_a, y_b = _yfrac(min(lats)), _yfrac(max(lats))
    sx = max((lon1 - lon0) / 360.0, 1e-4) * margin
    sy = max(abs(y_b - y_a), 1e-4) * margin
    z = max_zoom
    while z > min_zoom and (sx * _TILE_PX * (2 ** z) > max_w
                            or sy * _TILE_PX * (2 ** z) > max_h):
        z -= 1
    w = int(min(max(sx * _TILE_PX * (2 ** z), 360), max_w))
    h = int(min(max(sy * _TILE_PX * (2 ** z), 360), max_h))
    clon = (lon0 + lon1) / 2.0
    cy = (y_a + y_b) / 2.0
    clat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy))))
    return (clon, clat), z, w, h


def build_map_html(title, center, points, *, stats=None, zoom=None, grid=None,
                   width=1080, height=1080):
    """构建用于 html_render 的 MapLibre 页面。

    :param center: (lon, lat)
    :param points: [{lon, lat, name, sub, kind}]，kind ∈ target/near/player/ghost
    :param grid: 预取瓦片网格，N 或 (列, 行)；省略则按画幅自动计算
    :return: 完整 HTML 字符串（所有地图数据已内联为 base64）
    """
    if zoom is None:
        zoom = DEFAULT_ZOOM
    if not grid:
        grid = (int(math.ceil(width / _TILE_PX)) + 2,
                int(math.ceil(height / _TILE_PX)) + 2)

    js_points = []
    for p in points:
        label = str(p.get("name") or "")
        if p.get("sub"):
            label = (label + "  " + str(p["sub"])).strip()
        js_points.append({
            "lon": float(p["lon"]), "lat": float(p["lat"]),
            "kind": p.get("kind", "player"), "label": label,
        })
    data = {"center": [float(center[0]), float(center[1])],
            "zoom": float(zoom), "points": js_points}

    stats_html = "<br/>".join(_pct(s) for s in (stats or []))
    html = _PAGE
    html = html.replace("__CSS__", MAPLIBRE_CSS).replace("__JS__", MAPLIBRE_JS)
    html = html.replace("__W__", str(int(width))).replace("__H__", str(int(height)))
    html = html.replace("__TITLE__", _pct(title or ""))
    html = html.replace("__STATS__", stats_html)
    # 瓦片 / 海面 / 字体 三者并发抓取（都命中缓存时几乎瞬时）
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_tiles = ex.submit(_tiles_b64, center, zoom, grid)
        f_water = ex.submit(_load_water_b64)
        f_glyphs = ex.submit(_glyphs_b64)
        tiles_b64, water_b64, glyphs_b64 = f_tiles.result(), f_water.result(), f_glyphs.result()

    html = html.replace("__TILES__", tiles_b64)
    html = html.replace("__WATER__", water_b64)
    html = html.replace("__GLYPHS__", glyphs_b64)
    html = html.replace("__DATA__", base64.b64encode(json.dumps(data).encode()).decode())
    return html


# ============================================================
# 矢量 SVG 渲染（不依赖浏览器；道路/建筑/海面/城市/标记全为矢量路径，可无限放大）
# ============================================================

_WATER_DATA = {"obj": None}
# ---- 与官网 map.haulmp.com 样式逐项对齐（取自 live-map.js 当前启用的深色主题 i2()）----
_ROAD_FILL = {"freeway": "#b4bec2", "divided": "#a8b4bb"}
_ROAD_LOCAL = "#9aa7af"
_SVG_BG = "#29333a"          # background（陆地底色）
_SVG_WATER = "#15232e"       # water 图层
_SVG_CASING = "#202a31"      # road-casing 描边
_SVG_FERRY = "#617e94"       # ferries / trains
# areas / prefabs：官网按要素 color 属性取色（["match",["get","color"],...]）
_AREA_FILL = {0: "#9aa7af", 1: "#3a4348", 2: "#303a40", 3: "#2d3b35", 4: "#453c39",
              5: "#34463a", 6: "#293b47", 7: "#444940", 8: "#3c4046"}
_AREA_FILL_DEFAULT = "#394249"
_SVG_CITY_FG = "#d2dce3"
_SVG_CITY_HALO = "#253139"
_FONT = "'Microsoft YaHei',system-ui,Segoe UI,Helvetica,Arial,sans-serif"


def _water_data():
    if _WATER_DATA["obj"] is None:
        try:
            _WATER_DATA["obj"] = json.loads(base64.b64decode(_load_water_b64()))
        except Exception:
            _WATER_DATA["obj"] = {"type": "FeatureCollection", "features": []}
    return _WATER_DATA["obj"]


def _xml_esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _road_width(z):
    """道路线宽随缩放变化（量级与官网一致）。"""
    def _f(x0, y0, x1, y1, x):
        t = (x - x0) / (x1 - x0)
        f = (1.5 ** t - 1) / 0.5
        return y0 + (y1 - y0) * f
    if z <= 3:
        return 1.9
    if z >= 14:
        return 16.0
    return _f(3, 1.9, 14, 16.0, z)


def _exp15(z, stops):
    """MapLibre 的 interpolate(exponential 1.5) 分段插值（与官网线宽公式一致）。"""
    pts = sorted(stops)
    if z <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if z <= x1:
            f = (1.5 ** ((z - x0) / (x1 - x0)) - 1) / 0.5
            return y0 + (y1 - y0) * f
    return pts[-1][1]


def _casing_width(z):
    """官网 road-casing: interpolate(exp1.5, 3:0.7, 14:28, 16:140)。"""
    return _exp15(z, [(3, 0.7), (14, 28.0), (16, 140.0)])


def _fill_width(z):
    """官网 roads: interpolate(exp1.5, 3:0.546, 14:21.84, 16:109.2)。"""
    return _exp15(z, [(3, 0.546), (14, 21.84), (16, 109.2)])


def _area_opacity(z):
    """官网 areas/prefabs: interpolate(linear, 7:0.4, 10:1)。"""
    if z <= 7:
        return 0.4
    if z >= 10:
        return 1.0
    return 0.4 + (z - 7) / 3.0 * 0.6


def _ferry_opacity(z):
    """官网 ferries: interpolate(linear, 3:0.3, 7:0.65)。"""
    if z <= 3:
        return 0.3
    if z >= 7:
        return 0.65
    return 0.3 + (z - 3) / 4.0 * 0.35


def _city_size(z):
    """官网 cities（深色主题）: interpolate(linear, 4:11, 7:13, 11:16)。"""
    pts = [(4, 11.0), (7, 13.0), (11, 16.0)]
    if z <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if z <= x1:
            return y0 + (y1 - y0) * (z - x0) / (x1 - x0)
    return pts[-1][1]


def build_map_svg(title, center, points, *, stats=None, zoom=None, grid=None,
                  size=1080, width=None, height=None, tile_zoom=None):
    """服务端生成矢量 SVG 地图。返回 SVG 文本；失败返回 None。

    :param tile_zoom: 取瓦片的缩放级别，默认与 zoom 相同。设为 zoom+1 即
        「过采样」：用更高级别（细节更全）的瓦片铺满同样的视野，与官网
        MapLibre 放大时的做法一致，避免低级别瓦片路网被抽稀而与真实游戏
        地图错位。
    """
    if not _HAVE_MVT:
        return None
    if zoom is None:
        zoom = DEFAULT_ZOOM
    tz = int(tile_zoom) if tile_zoom is not None else int(zoom)
    tz = max(0, min(tz, _MAX_TILE_ZOOM))
    pt = 512.0 * (2.0 ** (zoom - tz))   # 每个瓦片在画布上的像素尺寸
    mw = int(width or size)
    mh = int(height or size)
    if not grid:
        grid = (int(math.ceil(mw / pt)) + 2, int(math.ceil(mh / pt)) + 2)
    if isinstance(grid, (tuple, list)):
        gw, gh = int(grid[0]), int(grid[1])
    else:
        gw = gh = int(grid)
    lon, lat = center
    n = 1 << tz
    fx, fy = _lonlat_to_frac(lon, lat, zoom)                 # 世界像素 = frac * 512
    gx, gy = fx * (2.0 ** (tz - zoom)), fy * (2.0 ** (tz - zoom))   # tz 下的瓦片坐标
    x0 = int(math.floor(gx)) - gw // 2
    y0 = int(math.floor(gy)) - gh // 2

    top_h = 56
    stats = list(stats or [])
    foot_h = 20 + 24 * max(1, len(stats))
    W = mw
    H = top_h + mh + foot_h
    shift = (mw / 2.0 - fx * 512.0, top_h + mh / 2.0 - fy * 512.0)
    rw = _fill_width(zoom)          # 官网 roads 线宽（所有 roadType 同一宽度，仅颜色区分）
    casing = _casing_width(zoom)    # 官网 road-casing 线宽（略宽，形成深色描边）

    def fmt(x, y):
        return "%d %d" % (round(x), round(y))

    def tvy(vy):
        """MVT 顶点 y(0..1) -> 瓦片内「自北边起算」的 y 归一化值。

        mapbox_vector_tile.decode() 默认 y_coord_down=False，会把瓦片里
        「y 向下」的坐标翻成「y 向上」返回，所以这里要再翻回来（1 - y）。
        少了这一步，每张瓦片的内容都会被上下镜像：路网在瓦片相接处断裂、
        建筑与真实地图错位（但标记与瓦片内部自洽，因此不易察觉）。
        """
        return 1.0 - vy

    # ---- 海面：官方 water 图层。每个多边形把「外环+内环」放进同一条路径并做奇偶填充，
    #      外环填海色、内环挖空由底层米色（陆地）透出 —— 与官网 MapLibre 的渲染一致 ----
    water_d = []
    for feat in _water_data().get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "MultiPolygon":
            continue
        for poly in geom.get("coordinates", []):
            subs = []
            for ring in poly:
                seq = []
                for c in ring:
                    wfx, wfy = _lonlat_to_frac(c[0], c[1], zoom)
                    seq.append(fmt(wfx * 512.0, wfy * 512.0))
                if len(seq) > 2:
                    subs.append("M" + " L".join(seq) + " Z")
            if subs:
                water_d.append(" ".join(subs))

    # ---- 瓦片内容 ----
    roads = {}
    prefabs = []
    ferries = []
    cities = []
    # 并发预取本图所需瓦片
    need = [(x0 + i, y0 + j) for i in range(gw) for j in range(gh)
            if 0 <= x0 + i < n and 0 <= y0 + j < n]
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda t: _fetch_tile(tz, t[0], t[1]), need))
    except Exception as e:
        logger.warning("瓦片预取异常: %s", e)
    for i in range(gw):
        for j in range(gh):
            tx, ty = x0 + i, y0 + j
            if tx < 0 or ty < 0 or tx >= n or ty >= n:
                continue
            raw = _fetch_tile(tz, tx, ty)
            if not raw:
                continue
            try:
                dec = _mvt.decode(raw)
            except Exception as e:
                logger.warning("瓦片解码失败 %d/%d/%d: %s", tz, tx, ty, e)
                continue
            lay = dec.get("ets2") or next(iter(dec.values()), None)
            if not lay:
                continue
            ext = lay.get("extent", 4096)
            for f in lay.get("features", []):
                g = f.get("geometry") or {}
                t = g.get("type")
                pr = f.get("properties") or {}
                typ = (pr.get("type") or "").lower()
                if t in ("LineString", "MultiLineString") and typ == "road":
                    if pr.get("hidden"):      # 官网 road-casing/roads 过滤 hidden
                        continue
                    rt = pr.get("roadType", "local")
                    rings = [g["coordinates"]] if t == "LineString" else g["coordinates"]
                    # 官网 roads 排除铁路；铁路与轮渡改由 ferries 图层画虚线
                    buf = ferries if rt == "train" else roads.setdefault(rt, [])
                    for ring in rings:
                        buf.append("M" + " L".join(
                            fmt((tx + v[0] / ext) * pt, (ty + tvy(v[1] / ext)) * pt)
                            for v in ring))
                elif t in ("LineString", "MultiLineString") and typ in ("ferry", "train"):
                    rings = [g["coordinates"]] if t == "LineString" else g["coordinates"]
                    for ring in rings:
                        ferries.append("M" + " L".join(
                            fmt((tx + v[0] / ext) * pt, (ty + tvy(v[1] / ext)) * pt)
                            for v in ring))
                elif t in ("Polygon", "MultiPolygon") and typ in ("prefab", "mapArea"):
                    polys = [g["coordinates"]] if t == "Polygon" else g["coordinates"]
                    col = _AREA_FILL.get(pr.get("color"), _AREA_FILL_DEFAULT)
                    zk = pr.get("zIndex", 0)
                    for ring in polys:
                        r0 = ring[0] if ring else []
                        if len(r0) > 2:
                            prefabs.append(("M" + " L".join(
                                fmt((tx + v[0] / ext) * pt, (ty + tvy(v[1] / ext)) * pt)
                                for v in r0) + " Z", col, zk))
                elif t == "Point" and typ == "city" and pr.get("name"):
                    cities.append(((tx + g["coordinates"][0] / ext) * pt,
                                   (ty + tvy(g["coordinates"][1] / ext)) * pt, pr["name"]))

    # ---- 组装 SVG ----
    o = []
    o.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
             'font-family="%s">' % (W, H, W, H, _FONT))
    o.append('<defs><clipPath id="mc"><rect x="0" y="%d" width="%d" height="%d"/></clipPath></defs>'
             % (top_h, mw, mh))
    o.append('<rect width="%d" height="%d" fill="#202a31"/>' % (W, H))
    o.append('<text x="16" y="38" fill="#ecf0fa" font-size="24" font-weight="600">%s</text>'
             % _xml_esc(title))
    # 陆地铺满地图区（屏幕坐标，不参与平移），海面由 water 图层奇偶填充叠加
    o.append('<rect x="0" y="%d" width="%d" height="%d" fill="%s"/>' % (top_h, mw, mh, _SVG_BG))
    o.append('<g clip-path="url(#mc)"><g transform="translate(%.2f,%.2f)">' % (shift[0], shift[1]))
    for d in water_d:
        o.append('<path d="%s" fill="%s" fill-rule="evenodd"/>' % (d, _SVG_WATER))
    # areas / prefabs：官网按 color 取色、按 zIndex 排序、透明度随缩放（minzoom 7）
    if zoom >= 7:
        aop = _area_opacity(zoom)
        for d, col, _zk in sorted(prefabs, key=lambda t: t[2]):
            o.append('<path d="%s" fill="%s" fill-opacity="%.2f"/>' % (d, col, aop))
    all_roads = " ".join(" ".join(v) for v in roads.values())
    if all_roads:
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.2f" '
                 'stroke-linecap="round" stroke-linejoin="round"/>'
                 % (all_roads, _SVG_CASING, casing))
    for rt, parts in roads.items():
        col = _ROAD_FILL.get(rt, _ROAD_LOCAL)
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%.2f" '
                 'stroke-linecap="round" stroke-linejoin="round"/>' % (" ".join(parts), col, rw))
    if ferries:
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="1" '
                 'stroke-opacity="%.2f" stroke-dasharray="2 5"/>'
                 % (" ".join(ferries), _SVG_FERRY, _ferry_opacity(zoom)))
    csz = _city_size(zoom)
    for cxx, cyy, nm in cities:
        o.append('<text x="%.1f" y="%.1f" font-size="%.1f" text-anchor="middle" fill="%s" '
                 'stroke="%s" stroke-width="2" paint-order="stroke">%s</text>'
                 % (cxx, cyy, csz, _SVG_CITY_FG, _SVG_CITY_HALO, _xml_esc(nm)))
    # 标签避让：近距离玩家的名字互相压叠时依次下移
    _placed = []

    def _est_w(s):
        return 8.0 * sum(1.0 if ord(c) < 0x2E80 else 1.8 for c in s)

    def _label_y(X, Y, est_w):
        y = Y
        for _ in range(16):
            hit = [ly for (a, b, ly) in _placed
                   if abs(y - ly) < 17 and not (X + est_w < a or X > b)]
            if not hit:
                break
            y = max(hit) + 17
        _placed.append((X, X + est_w, y))
        return y

    # 标记
    for p in points:
        pfx, pfy = _lonlat_to_frac(p["lon"], p["lat"], zoom)
        X, Y = pfx * 512.0, pfy * 512.0
        kind = p.get("kind", "player")
        label = str(p.get("name") or "")
        if p.get("sub"):
            label = (label + "  " + str(p["sub"])).strip()
        brg = p.get("bearing")      # 朝向（官网同款箭头）；None 时退回圆点
        if kind == "target":
            if brg is None:
                o.append('<path d="M%.1f %.1f L%.1f %.1f A9 9 0 1 1 %.1f %.1f Z" fill="#dc3232" '
                         'stroke="#ffffff" stroke-width="2"/>'
                         % (X, Y, X - 9, Y - 14, X + 9, Y - 14))
                o.append('<circle cx="%.1f" cy="%.1f" r="9" fill="#dc3232" stroke="#ffffff" '
                         'stroke-width="2"/>' % (X, Y - 14))
                o.append('<circle cx="%.1f" cy="%.1f" r="3.6" fill="#ffffff"/>' % (X, Y - 14))
            else:
                # 箭头：尖端在锚点(玩家位置)，翼向后（rotate(0)=正北朝上）
                o.append('<g transform="translate(%.1f,%.1f) rotate(%.1f)">'
                         '<path d="M0 0 L11 21 L0 15 L-11 21 Z" fill="#dc3232" '
                         'stroke="#ffffff" stroke-width="1.4" stroke-linejoin="round"/></g>'
                         % (X, Y, brg))
            o.append('<text x="%.1f" y="%.1f" font-size="15" fill="#ffffff" stroke="#0b1220" '
                     'stroke-width="3.4" paint-order="stroke">%s</text>'
                     % (X + 14, _label_y(X + 14, Y - 12, _est_w(label) * 1.07),
                        _xml_esc(label)))
        else:
            if kind == "near":
                fc, sc = "#3ca7ff", "#e0f1ff"
            elif kind == "ghost":
                fc, sc = "#9fb0c4", "#e8eef4"
            else:
                fc, sc = "#3ca7ff", "#e0f1ff"
            if brg is None:
                o.append('<circle cx="%.1f" cy="%.1f" r="5" fill="%s" stroke="%s" '
                         'stroke-width="1.4"/>' % (X, Y, fc, sc))
            else:
                o.append('<g transform="translate(%.1f,%.1f) rotate(%.1f)">'
                         '<path d="M0 0 L8 15 L0 10.5 L-8 15 Z" fill="%s" stroke="%s" '
                         'stroke-width="1" stroke-linejoin="round"/></g>'
                         % (X, Y, brg, fc, sc))
            if kind == "near" and label:
                o.append('<text x="%.1f" y="%.1f" font-size="14" fill="#d2dce3" stroke="#253139" '
                         'stroke-width="3" paint-order="stroke">%s</text>'
                         % (X + 9, _label_y(X + 9, Y + 5, _est_w(label)), _xml_esc(label)))
    o.append('</g></g>')
    # 底部信息
    o.append('<rect x="0" y="%d" width="%d" height="%d" fill="#202a31"/>'
             % (top_h + mh, W, foot_h))
    y = top_h + mh + 22
    for s in stats:
        o.append('<text x="16" y="%d" fill="#a8b4bb" font-size="16">%s</text>' % (y, _xml_esc(s)))
        y += 24
    o.append('</svg>')
    return "\n".join(o)

