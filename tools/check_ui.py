"""Headless UI smoke test with an in-memory Plugin View bridge fixture.

Run: uv run --group browser python -m tools.check_ui
Uses the installed Microsoft Edge; no remote images or credentials are needed.
"""

import asyncio
import io
import json
from pathlib import Path

from aiohttp import web
from PIL import Image
from playwright.async_api import async_playwright

from backend.models import Settings

ROOT = Path(__file__).resolve().parents[1]

BRIDGE = r"""
(() => {
 const settings = SETTINGS;
 const items = Array.from({length: 8}, (_, i) => ({
  id: i === 0 ? '9223372036854775807' : String(i + 1), text: ['确实','开心','吃瓜','摸鱼','收到','疑惑','晚安','加油'][i],
  rating: 'none', image_url: 'https://r2.longhub.top/test/' + i + '.png',
  hub_tags: ['赞同'], ai_tags: ['开心','交流'], manual_tags: [], excluded_tags: [],
  effective_tags: ['赞同','开心'], description: '用于对话回应', ai_description: '用于对话回应',
  manual_description: null, enabled: true, annotation_fresh: true, tag_job: {status:'done',error:null}
 }));
 const status = {total:8,pending_notifications:0,tag_jobs:{queued:2,running:0,done:6,failed:0},
  tagging_paused:false,ready:true,receiver:{started:true,error:null,listen:'127.0.0.1:8791',path:'/hooks/longhub'},
  sync:{status:'done',record_count:8},last_sync:{completed_at:1792886400},last_webhook:1792886460,vision_configured:true};
 const logs = [{id:1,received:1792886460,image_id:'9223372036854775807',event:'update',
  signature_valid:true,http_status:204,processed:true,attempts:0,body:'{"event":"update"}'}];
 window.__lastSaved = null;
 window.AstrBotPluginView = {
  ready: async () => ({}),
  apiGet: async (path, params={}) => {
   if (path === 'status') return structuredClone(status);
   if (path === 'settings') return {settings:structuredClone(settings),providers:[{id:'vision',name:'vision'}]};
   if (path === 'memes') {
    const list = items.filter(x => (!params.q || x.text.includes(params.q) || x.effective_tags.some(t=>t.includes(params.q))) && (!params.enabled || String(x.enabled)===params.enabled));
    return {items:structuredClone(list),total:list.length,page:1};
   }
   if (path.startsWith('memes/')) return structuredClone(items.find(x=>x.id===path.split('/')[1]));
   if (path === 'webhook/logs') return {items:logs,total:1,page:1};
   throw new Error('Unexpected GET ' + path);
  },
  apiPost: async (path, body) => {
   if (path.startsWith('memes/') && path.endsWith('/edit')) {
    const item=items.find(x=>x.id===path.split('/')[1]);Object.assign(item,body);
    item.effective_tags=[...item.hub_tags,...item.ai_tags,...item.manual_tags].filter(x=>!item.excluded_tags.includes(x));return item;
   }
   if (path==='settings/save') {window.__lastSaved=body;Object.assign(settings,body);return {saved:true};}
   if (path==='tagging/pause') {status.tagging_paused=body.paused;return {paused:body.paused};}
   if (path==='tagging/enqueue') return {queued:body.ids?.length || 8};
   if (path==='sync') return {id:'test-sync',status:'queued'};
   if (path==='webhook/retry') return {queued:true};
   throw new Error('Unexpected POST ' + path);
  }
 };
})();
"""


async def check():
    app = web.Application()
    app.router.add_static("/", ROOT / "views" / "catalog", show_index=True)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/index.html"
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "#e5eee8").save(buffer, "PNG")
    png = buffer.getvalue()
    output = ROOT / ".preview-data"
    output.mkdir(exist_ok=True)
    settings = Settings(vision_provider_id="vision").public()
    errors = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": 1440, "height": 1100}, device_scale_factor=1
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.add_init_script(BRIDGE.replace("SETTINGS", json.dumps(settings)))
                await page.route(
                    "https://r2.longhub.top/**",
                    lambda route: route.fulfill(body=png, content_type="image/png"),
                )
                await page.goto(url)
                await page.locator("#connection-state").filter(has_text="已连接接收器").wait_for()
                await page.screenshot(path=str(output / "overview.png"), full_page=True)
                await page.locator('[data-tab="catalog"]').click()
                await page.wait_for_function("document.querySelectorAll('.meme-card').length === 8")
                await page.locator(".card-image").first.click()
                await page.locator("#meme-dialog[open]").wait_for()
                assert "9223372036854775807" in await page.locator("#meme-title").inner_text()
                await page.locator("#manual-tags").fill("手工, <script>alert(1)</script>")
                await page.locator(
                    "#edit-form button[type=submit], #edit-form .primary"
                ).first.click()
                await page.wait_for_function("!document.querySelector('#meme-dialog').open")
                await page.locator(".card-image").first.click()
                await page.locator("#manual-tags").wait_for()
                assert "手工" in await page.locator("#manual-tags").input_value()
                await page.locator('[data-close="meme-dialog"]').click()
                await page.locator('[data-tab="logs"]').click()
                await page.locator("#log-rows button").first.click()
                await page.locator("#log-dialog[open]").wait_for()
                assert "9223372036854775807" in await page.locator("#log-detail").inner_text()
                await page.locator('[data-close="log-dialog"]').click()
                await page.locator('[data-tab="tasks"]').click()
                await page.locator("#pause-tagging").click()
                await page.locator("#pause-tagging").filter(has_text="继续队列").wait_for()
                await page.locator('[data-tab="settings"]').click()
                await page.locator('[name="tagging_rpm"]').fill("9")
                await page.locator("#settings-form button[type=submit]").click()
                await page.wait_for_function("window.__lastSaved?.tagging_rpm === 9")
                saved = await page.evaluate("window.__lastSaved")
                assert "app_key" not in saved
                await page.locator('[data-tab="overview"]').click()
                await page.evaluate("document.documentElement.dataset.theme='dark'")
                await page.screenshot(path=str(output / "overview-dark.png"), full_page=True)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.evaluate("document.documentElement.dataset.theme='light'")
                await page.screenshot(path=str(output / "overview-mobile.png"), full_page=True)
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                assert not errors, errors
                print(
                    "UI checks passed: desktop/mobile, dark theme, catalog edit, int64, logs, tasks, settings"
                )
                print(output / "overview.png")
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(check())
