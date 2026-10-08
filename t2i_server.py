"""
本地「等待式」t2i 服务（用于 astrbot_plugin_hmp_bot 的路况 / 定位路网渲染）

安装：
    pip install playwright
    playwright install chromium      # 仅首次需要，会下载 Chromium（约 150MB）

运行：
    python t2i_server.py            # 默认监听 http://127.0.0.1:8787
    T2I_PORT=9000 python t2i_server.py

然后在插件 _conf_schema.json 的 t2i_endpoint 填：http://127.0.0.1:8787
（AStrBot 会向其 POST /generate；也可填 http://127.0.0.1:8787/text2img 以兼容默认路径）

说明：本服务需要 Chromium，运行在能访问 map.haulmp.com 的机器上（即部署 AstrBot 的本机）。
不配置 / 不启用时，插件会回退到默认远程端点（路网可能不显示）或文字输出，不影响其它功能。
"""

import asyncio
import contextlib
import os

from aiohttp import web

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

PORT = int(os.environ.get("T2I_PORT", "8787"))
_DOM_DUMP_COUNTER = 0


def _find_chrome():
    """优先使用本机已有的 Chromium / Chrome，避免再下载浏览器。"""
    import glob

    candidates = [
        os.environ.get("CHROMIUM_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    pw_dir = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    if os.path.isdir(pw_dir):
        candidates.extend(
            glob.glob(os.path.join(pw_dir, "chromium-*", "chrome-win64", "chrome.exe"))
        )
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _write_text(path: str, text: str) -> None:
    """把文本写入文件；供 asyncio.to_thread 调用，避免阻塞事件循环。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


async def render_html(
    html,
    typ="jpeg",
    quality=90,
    width=720,
    height=720,
    full_page=True,
    scale=2,
    print_stats=False,
    debug_tag="",
):
    if async_playwright is None:
        raise RuntimeError("未安装 playwright，请先执行：pip install playwright")
    exe = _find_chrome()
    # --disable-web-security：HaulMP 矢量瓦片未配置 CORS，本地 t2i 必须绕过跨域才能加载路网
    # 注意：Playwright 的 launch() 不接受 --user-data-dir，需用 launch_persistent_context；
    # 这里直接省略，让 Playwright 使用临时 profile 即可生效。
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-web-security",
    ]
    launch_kwargs = {"args": args}
    if exe:
        launch_kwargs["executable_path"] = exe
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        page = await browser.new_page(
            viewport={"width": int(width), "height": int(height)},
            device_scale_factor=int(scale),
        )
        # 注入浏览器 UA，避免部分地图接口按 UA 拦截
        await page.set_extra_http_headers(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            }
        )
        # 捕获页面控制台与 JS 错误，便于定位 vectorgrid 渲染异常
        page.on(
            "console", lambda msg: print(f"[browser console] {msg.type}: {msg.text}")
        )
        page.on("pageerror", lambda exc: print(f"[browser pageerror] {exc}"))
        # 防御：若上游未做 Jinja 解析，html 可能仍被 {% raw %} 包裹；先剥离避免文字残留，
        # 同时防止二次 set_content 导致 JS const 重复声明而整段脚本崩溃。
        if html.startswith("{% raw %}") and html.endswith("{% endraw %}"):
            html = html[len("{% raw %}") : -len("{% endraw %}")]
        await page.set_content(html, wait_until="domcontentloaded")
        # 等瓦片 / 字体等网络请求空闲（最多 15s），这是路网能显示的关键
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:  # 瓦片多时可能等不到空闲，不算失败，下面继续轮询等瓦片
            print(f"[t2i_server] 等待网络空闲超时（继续等待瓦片加载）: {e}")
        # 动态等待矢量瓦片真正加载完成（HaulMP 矢量路网）
        # 网络空闲后再最多轮询 10s，直到至少有一块瓦片加载成功
        max_wait_ms, poll_interval_ms = 10000, 300
        waited, base_ok = 0, False
        while waited < max_wait_ms:
            try:
                stats = await page.evaluate(
                    "(() => { const b = window.__base; return b ? "
                    "{loaded:b.loaded, errored:b.errored, ok:b.ok} : null; })()"
                )
            except Exception:
                stats = None
            if stats:
                print(
                    f"[t2i_server] 瓦片统计 loaded={stats.get('loaded')} errored={stats.get('errored')} ok={stats.get('ok')}"
                )
                if stats.get("loaded", 0) > 0:
                    base_ok = True
                    break
            await page.wait_for_timeout(poll_interval_ms)
            waited += poll_interval_ms
        if not base_ok:
            print(
                "[t2i_server] 警告：矢量瓦片未加载成功，路网可能不会显示（请检查 CORS/网络/瓦片地址）"
            )
        # 额外给矢量瓦片/ SVG 绘制更多时间（部分环境渲染慢）
        await page.wait_for_timeout(1500)
        # 输出样式诊断：确认 vStyle 是否被调用、以及瓦片属性
        try:
            diag = await page.evaluate(
                "(() => { "
                "  const allPaths = document.querySelectorAll('path'); "
                "  const allCircles = document.querySelectorAll('circle'); "
                "  const allSvg = document.querySelectorAll('svg'); "
                "  const allCanvas = document.querySelectorAll('canvas'); "
                "  const vectorTiles = document.querySelectorAll('.leaflet-vector-tile, .leaflet-tile'); "
                "  const overlayPane = document.querySelector('.leaflet-overlay-pane'); "
                "  return { styleCalls: window.__styleCalls || 0, roadCount: window.__roadCount || 0, "
                "           firstProps: window.__firstProps, allPathCount: allPaths.length, "
                "           allCircleCount: allCircles.length, allSvgCount: allSvg.length, "
                "           allCanvasCount: allCanvas.length, vectorTileCount: vectorTiles.length, "
                "           overlayHtmlLength: overlayPane ? overlayPane.innerHTML.length : -1 }; "
                "})()"
            )
            print(
                f"[t2i_server] 样式诊断 styleCalls={diag.get('styleCalls')} roadCount={diag.get('roadCount')} firstProps={diag.get('firstProps')}"
            )
            print(
                f"[t2i_server] 元素统计 allPathCount={diag.get('allPathCount')} allCircleCount={diag.get('allCircleCount')} allSvgCount={diag.get('allSvgCount')} allCanvasCount={diag.get('allCanvasCount')} vectorTileCount={diag.get('vectorTileCount')} overlayHtmlLength={diag.get('overlayHtmlLength')}"
            )
        except Exception as e:
            print("[t2i_server] 读取样式诊断失败:", e)
        # 可选调试：把最终 DOM 保存到本地，便于排查 SVG/Canvas 是否生成。
        # 默认关闭，避免每次出图都在插件目录里落文件；需要时设置环境变量 T2I_DEBUG_DOM=1。
        if debug_tag and os.environ.get("T2I_DEBUG_DOM", "") == "1":
            try:
                dom = await page.content()
                dump_path = os.path.join(
                    os.path.dirname(__file__), f"debug_dom_{debug_tag}.html"
                )
                await asyncio.to_thread(_write_text, dump_path, dom)
                print(f"[t2i_server] DOM 已保存: {dump_path}")
            except Exception as e:
                print("[t2i_server] 保存 DOM 失败:", e)
        # 兼容旧参数：print_stats 为 True 时额外再打印一次最终统计
        if print_stats and base_ok:
            print("[t2i_server] 最终瓦片统计:", stats)
        if str(typ).lower() in ("jpg", "jpeg"):
            img = await page.screenshot(
                full_page=bool(full_page), type="jpeg", quality=int(quality)
            )
            ctype = "image/jpeg"
        else:
            img = await page.screenshot(full_page=bool(full_page), type="png")
            ctype = "image/png"
        await browser.close()
        return img, ctype


async def handle_generate(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    html = data.get("html", "")
    if not html:
        return web.Response(status=400, text="missing html")
    typ = data.get("type", "jpeg")
    quality = int(data.get("quality", 90))
    width = int(data.get("width", 720))
    height = int(data.get("height", 720))
    full_page = bool(data.get("full_page", True))
    global _DOM_DUMP_COUNTER
    _DOM_DUMP_COUNTER += 1
    try:
        img, ctype = await render_html(
            html,
            typ,
            quality,
            width,
            height,
            full_page,
            debug_tag=f"{_DOM_DUMP_COUNTER:03d}",
        )
    except Exception as e:
        return web.Response(status=500, text=f"render error: {e}")
    return web.Response(body=img, content_type=ctype)


async def main():
    if async_playwright is None:
        print("[t2i_server] 警告：未安装 playwright，服务无法渲染。请执行：")
        print("  pip install playwright && playwright install chromium")
    app = web.Application()
    app.router.add_post("/generate", handle_generate)
    app.router.add_post("/text2img/generate", handle_generate)  # 兼容默认路径
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    print(f"[t2i_server] 已启动：http://127.0.0.1:{PORT}  （POST /generate 渲染地图）")
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        await asyncio.Event().wait()  # 常驻运行，直到收到 Ctrl+C / 取消信号


if __name__ == "__main__":
    asyncio.run(main())
# 文件用途：本地等待式 t2i 渲染服务 —— 用 Playwright 截图路况 / 定位地图模板并返回图片
