#!/usr/bin/env node
/**
 * Leaflet + Puppeteer 地图渲染器（HaulMP 插件可选渲染后端）。
 *
 * 用法：node leaflet_render.js <template.html> <out.png> <data.json>
 *   - template.html : leaflet_templates/locate.html 或 traffic.html
 *   - out.png       : 输出 PNG 路径
 *   - data.json     : 渲染数据（{title, center:[lon,lat], points:[...], stats, tileUrl, tileType}）
 *
 * 依赖：puppeteer（npm install puppeteer，会下载 Chromium）。
 * 说明：为兼容 HaulMP 矢量瓦片（可能无 CORS），启动 Chromium 时关闭了 web-security。
 */
'use strict';

const fs = require('fs');
const path = require('path');

async function main() {
  const [, , template, outPng, dataJson] = process.argv;
  if (!template || !outPng || !dataJson) {
    console.error('用法: node leaflet_render.js <template.html> <out.png> <data.json>');
    process.exit(2);
  }
  if (!fs.existsSync(template) || !fs.existsSync(dataJson)) {
    console.error('模板或数据文件不存在');
    process.exit(3);
  }

  let puppeteer;
  try {
    puppeteer = require('puppeteer');
  } catch (e) {
    console.error('未安装 puppeteer，请先在本目录执行: npm install puppeteer');
    process.exit(4);
  }

  const data = JSON.parse(fs.readFileSync(dataJson, 'utf8'));

  const browser = await puppeteer.launch({
    headless: 'new',
    args: [
      '--no-sandbox', '--disable-setuid-sandbox',
      '--disable-web-security', '--allow-running-insecure-content',
      '--disable-gpu', '--disable-dev-shm-usage'
    ],
    ignoreHTTPSErrors: true
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 720, height: 856, deviceScaleFactor: 1 });
    // file:// 加载模板（内部相对路径 ./vendor/ 基于模板所在目录）
    await page.goto('file://' + path.resolve(template), { waitUntil: 'load', timeout: 30000 });
    await page.evaluate('window.setData(' + JSON.stringify(data) + ')');

    // 等瓦片/热力绘制：优先网络空闲，超时则退化为固定等待
    try {
      await page.waitForNetworkIdle({ idleTime: 600, timeout: 8000 });
    } catch (_) { /* 忽略超时，继续 */ }
    await new Promise(r => setTimeout(r, 1500));

    const el = await page.$('#container');
    if (!el) throw new Error('#container 元素不存在');

    // 底图状态：无底图或瓦片加载失败时改用文字输出，不再出合成暗色画布。
    const base = await page.evaluate(() => window.__base || { ok: false, loaded: 0, errored: 0 });
    if (!base.ok || (base.errored > 0 && base.loaded === 0)) {
      throw new Error('底图加载失败，已改用文字输出');
    }

    await el.screenshot({ path: outPng, type: 'png' });
    console.log('OK ' + outPng);
  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error('渲染失败:', err && err.message ? err.message : err);
  process.exit(1);
});
