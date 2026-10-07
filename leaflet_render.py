"""Leaflet + Puppeteer 渲染后端（HaulMP 插件地图渲染器）。

渲染入口 render_map 签名：
    render_map(title, center, points, *, stats=None, out_path=None, mode="auto", tile_url="", tile_type="auto")

- center: (lon, lat) 主视角中心
- points: [{lon, lat, name, kind, sub}]，kind ∈ target/near/player/ghost
- mode: "locate"（标记）/ "traffic"（热力）/ "auto"（按是否含 target 推断）
- tile_url / tile_type: 底图瓦片。留空则使用 HaulMP 官方矢量瓦片（真实 ETS2 路网 .pbf）；
  tile_type ∈ auto/raster/vector。底图加载失败时由调用方改用文字输出（不再出合成底图）。

前置：本目录需 `npm install puppeteer`（会下载 Chromium），且系统 PATH 中有 node。
失败时抛出 RuntimeError，由调用方（main.py）改用文字摘要输出。
"""

import json
import os
import shutil
import subprocess
import tempfile

logger = None  # 延迟导入，避免循环依赖

_HERE = os.path.dirname(os.path.abspath(__file__))
_NODE_SCRIPT = os.path.join(_HERE, "leaflet_render.js")
_TPL_LOCATE = os.path.join(_HERE, "leaflet_templates", "locate.html")
_TPL_TRAFFIC = os.path.join(_HERE, "leaflet_templates", "traffic.html")


def _infer_mode(points):
    for p in points:
        if (p.get("kind") or "") == "target":
            return "locate"
    return "traffic"


def render_map(title, center, points, *, stats=None, out_path=None,
               mode="auto", tile_url="", tile_type="auto"):
    """用 Leaflet + Puppeteer 把玩家位置渲染成 PNG，返回输出路径。"""
    if mode == "auto":
        mode = _infer_mode(points)
    tpl = _TPL_LOCATE if mode == "locate" else _TPL_TRAFFIC

    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 node 可执行文件，无法使用 Leaflet 渲染")
    if not (os.path.exists(_NODE_SCRIPT) and os.path.exists(tpl)):
        raise RuntimeError("Leaflet 渲染脚本或模板缺失")

    if out_path is None:
        out_path = os.path.join(tempfile.gettempdir(), f"hmp_leaflet_{os.getpid()}.png")

    data = {
        "title": title,
        "center": [center[0], center[1]],  # [lon, lat]
        "points": [
            {
                "lon": float(p["lon"]), "lat": float(p["lat"]),
                "name": p.get("name") or "", "kind": p.get("kind", "player"),
                "sub": p.get("sub") or "",
            }
            for p in points
        ],
        "stats": stats or [],
        "tileUrl": tile_url or "",
        "tileType": tile_type or "auto",
    }

    data_path = out_path + ".json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    try:
        proc = subprocess.run(
            [node, _NODE_SCRIPT, tpl, out_path, data_path],
            cwd=_HERE, capture_output=True, text=True, timeout=90,
        )
    finally:
        try:
            os.remove(data_path)
        except OSError:
            pass

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = msg[-1] if msg else f"exit={proc.returncode}"
        raise RuntimeError(f"Leaflet 渲染失败: {msg}")

    if not os.path.exists(out_path):
        raise RuntimeError("Leaflet 渲染未生成图片")
    return os.path.abspath(out_path)
