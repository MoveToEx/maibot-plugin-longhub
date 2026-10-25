"""Edge acceptance checks for the built SPA, using only in-memory bridge/media fixtures."""

import asyncio
import html
import json
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright, expect

from backend.models import Settings

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/longhub/views/catalog/_t/test-session/"

BRIDGE = r"""
(() => {
 const settings = SETTINGS;
 settings.app_key_configured = true; settings.webhook_secret_configured = true; settings.embedding_provider_id='embedding';
 const items = Array.from({length:65}, (_,i) => ({
  id:i===0?'9223372036854775807':String(i+1),
  text:['确实','开心','吃瓜','摸鱼','收到','疑惑','晚安','加油'][i%8],
  rating:i%9===8?'moderate':'none',image_url:'https://r2.longhub.top/test/'+i+'.svg',
  hub_tags:['genshin_impact','happy'],ai_tags:['龙脸','开心'],manual_tags:[],excluded_tags:[],
  effective_tags:['genshin_impact','happy','龙脸','开心'],description:'带龙脸的角色，用于轻松回应对话。',
  ai_description:'带龙脸的角色，用于轻松回应对话。',manual_description:null,
  enabled:i!==4,annotation_fresh:i%5!==0,tag_job:{status:'done',error:null}
 }));
 const status={total:65,ratings:{none:58,moderate:7,violent:0},pending_notifications:2,
  tag_jobs:{queued:12,running:1,done:48,failed:4},tagging_paused:false,ready:true,
  receiver:{started:true,error:null,listen:'127.0.0.1:8791',path:'/hooks/longhub'},
  sync:{status:'done'},last_sync:{completed_at:1792886400},last_webhook:1792886460,vision_configured:true,
  semantic:{configured:true,provider_id:'embedding',model:'embedding-demo',total:65,annotated:49,coverage:49/65,indexed:49,pending:0,available:true,state:'ready',error:null}};
 const logs=[
  {id:1,received:1792886460,image_id:'9223372036854775807',event:'update',signature_valid:true,
   http_status:204,processed:true,attempts:0,body:'{"event":"update"}'},
  {id:2,received:1792886400,image_id:'3',event:'create',signature_valid:true,
   http_status:204,processed:false,attempts:2,processing_error:'Upstream unavailable',body:'{}'},
  {id:3,received:1792886300,image_id:null,event:null,signature_valid:false,
   http_status:401,processed:false,attempts:0,error:'Signature mismatch',body:''}
 ];
 window.__lastSaved=null;window.__calls=[];window.__failNext=null;
 window.AstrBotPluginView={
  ready:async()=>({}),
  apiGet:async(path,params={})=>{
   window.__calls.push({method:'GET',path,params:structuredClone(params)});
   if(window.__failNext===path){window.__failNext=null;throw new Error('测试读取失败，请重试');}
   if(path==='status')return structuredClone(status);
   if(path==='settings')return {settings:structuredClone(settings),providers:[{id:'vision',name:'视觉模型'}],embedding_providers:[{id:'embedding',name:'嵌入模型'}]};
   if(path==='memes'){
    const list=items.filter(x=>(!params.q||x.text.includes(params.q)||x.effective_tags.some(t=>t.includes(params.q)))&&
     (!params.enabled||String(x.enabled)===params.enabled)&&(!params.rating||x.rating===params.rating)&&
     (!params.annotation||!x.annotation_fresh));
    const page=Number(params.page||1),limit=Number(params.limit||30);
    return {items:structuredClone(list.slice((page-1)*limit,page*limit)),total:list.length,page};
   }
   if(path.startsWith('memes/')){
    const item=items.find(x=>x.id===path.split('/')[1]);if(!item)throw new Error('目录中没有这张图片');
    return structuredClone(item);
   }
   if(path==='webhook/logs'){
    const list=logs.filter(x=>(!params.event||x.event===params.event)&&
     (params.failed!=='true'||x.http_status!==204||x.processing_error));
    return {items:structuredClone(list),total:list.length,page:1};
   }
   throw new Error('Unexpected GET '+path);
  },
  apiPost:async(path,body={})=>{
   window.__calls.push({method:'POST',path,body:structuredClone(body)});
   if(path.startsWith('memes/')&&path.endsWith('/edit')){
    const item=items.find(x=>x.id===path.split('/')[1]);Object.assign(item,body,{manual_description:body.description});
    item.description=body.description??item.ai_description;
    item.effective_tags=[...new Set([...item.hub_tags,...item.ai_tags,...item.manual_tags])].filter(x=>!item.excluded_tags.includes(x));
    return structuredClone(item);
   }
   if(path==='settings/save'){window.__lastSaved=structuredClone(body);Object.assign(settings,body);return {saved:true};}
   if(path==='tagging/pause'){status.tagging_paused=body.paused;return {paused:body.paused};}
   if(path==='tagging/enqueue')return {queued:body.ids?.length||16};
   if(path==='sync')return {id:'test-sync',status:'queued'};
   if(path==='webhook/retry')return {queued:true};
   if(path==='semantic/rebuild')return {queued:true};
   throw new Error('Unexpected POST '+path);
  }
 };
})();
"""


async def check():
    @web.middleware
    async def cors(request, handler):
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    app = web.Application(middlewares=[cors])
    app.router.add_static(PREFIX, ROOT / "views" / "catalog")

    async def sandbox(_):
        return web.Response(
            text=f'<iframe title="Plugin View" sandbox="allow-scripts allow-forms allow-downloads" '
            f'src="{PREFIX}index.html#/catalog" style="border:0;width:100%;height:950px"></iframe>',
            content_type="text/html",
        )

    app.router.add_get("/sandbox.html", sandbox)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    url = origin + PREFIX + "index.html"
    output = ROOT / ".preview-data"
    output.mkdir(exist_ok=True)
    settings = Settings(vision_provider_id="vision").public()
    errors = []

    async def media(route):
        index = int(route.request.url.rsplit("/", 1)[1].split(".")[0])
        colors = ["#ece8f7", "#f2e9da", "#dfede7", "#e4eaf4"]
        words = ["确实", "开心", "吃瓜", "摸鱼", "收到", "疑惑", "晚安", "加油"]
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="280">
        <rect x="80" y="22" width="240" height="230" rx="60" fill="{colors[index % 4]}"/>
        <circle cx="174" cy="102" r="5" fill="#5e566c"/><circle cx="226" cy="102" r="5" fill="#5e566c"/>
        <path d="M175 133 Q200 152 225 133" fill="none" stroke="#5e566c" stroke-width="4" stroke-linecap="round"/>
        <text x="200" y="202" text-anchor="middle" font-family="Microsoft YaHei,sans-serif" font-size="35" fill="#5e566c">{html.escape(words[index % 8])}</text></svg>'''
        await route.fulfill(body=svg, content_type="image/svg+xml")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": 1440, "height": 1100}, reduced_motion="reduce"
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.add_init_script(BRIDGE.replace("SETTINGS", json.dumps(settings)))
                await page.route("https://r2.longhub.top/**", media)
                await page.goto(url)
                await expect(page.locator("#connection-state")).to_have_text("已连接接收器")
                await page.screenshot(path=str(output / "overview.png"), full_page=True)
                await page.locator('[data-tab="catalog"]').click()
                await expect(page.locator(".meme-card")).to_have_count(30)
                await page.get_by_role("button", name="下一页", exact=True).click()
                await expect(page).to_have_url(url + "#/catalog?page=2")
                await page.reload()
                await expect(page.locator(".meme-card")).to_have_count(30)
                await expect(page.locator(".pagination")).to_contain_text("2 / 3")
                await page.get_by_role("button", name="上一页", exact=True).click()
                await page.locator(".card-image").first.click()
                await expect(page.get_by_role("dialog")).to_contain_text("9223372036854775807")
                await page.locator("#manual-tags").fill("手工, <script>alert(1)</script>")
                await page.get_by_role("switch", name="使用模型描述", exact=True).click()
                await page.locator("#manual-description").fill("人工修订的描述")
                await (
                    page.get_by_role("dialog")
                    .get_by_role("button", name="happy", exact=True)
                    .click()
                )
                await page.screenshot(path=str(output / "meme-detail.png"))
                await page.get_by_role("button", name="保存修订", exact=True).click()
                await expect(page.get_by_role("dialog")).to_have_count(0)
                await page.locator(".card-image").first.click()
                await expect(page.locator("#manual-tags")).to_have_value(
                    "手工, <script>alert(1)</script>"
                )
                await expect(page.locator("#manual-description")).to_have_value("人工修订的描述")
                await expect(page.locator("#excluded-tags")).to_have_value("happy")
                await page.keyboard.press("Escape")
                await expect(page.get_by_role("dialog")).to_have_count(0)
                await page.locator("#query").fill("不存在")
                await page.locator("#search-form button").click()
                await expect(page.get_by_text("没有找到合适的表情", exact=True)).to_be_visible()
                await page.get_by_role("button", name="清除筛选", exact=True).first.click()
                await expect(page.locator(".meme-card")).to_have_count(30)
                await page.get_by_role("combobox", name="分级筛选", exact=True).click()
                await page.get_by_role("option", name="moderate", exact=True).click()
                await expect(page.locator(".meme-card")).to_have_count(7)
                await page.go_back()
                await expect(page.locator(".meme-card")).to_have_count(30)
                await page.go_forward()
                await expect(page.locator(".meme-card")).to_have_count(7)
                await page.get_by_role("button", name="清除筛选", exact=True).click()
                await expect(page.locator(".meme-card")).to_have_count(30)
                await page.get_by_role(
                    "checkbox", name="选择表情 9223372036854775807", exact=True
                ).check()
                await page.locator("#tag-selected").click()
                await page.wait_for_function(
                    "window.__calls.some(x=>x.method==='POST'&&x.path==='tagging/enqueue'&&x.body.ids?.[0]==='9223372036854775807')"
                )
                await page.evaluate("window.__failNext='memes'")
                await page.locator("#refresh").click()
                await expect(page.get_by_role("alert")).to_contain_text("测试读取失败")
                await page.get_by_role("button", name="重试", exact=True).click()
                await expect(page.locator(".meme-card")).to_have_count(30)
                await page.screenshot(path=str(output / "catalog.png"))
                await page.locator('[data-tab="logs"]').click()
                await expect(page.locator("#log-rows tr")).to_have_count(3)
                await page.get_by_role("button", name="查看日志 1", exact=True).click()
                await expect(page.locator("#log-detail")).to_contain_text("9223372036854775807")
                await page.keyboard.press("Escape")
                await page.get_by_role("checkbox", name="只看失败", exact=True).check()
                await expect(page.locator("#log-rows tr")).to_have_count(2)
                await page.locator('[data-tab="tasks"]').click()
                await page.locator("#pause-tagging").click()
                await expect(page.locator("#pause-tagging")).to_contain_text("继续队列")
                await expect(
                    page.get_by_role("progressbar", name="有效标注覆盖率")
                ).to_have_attribute("aria-valuenow", "75")
                await page.locator("#rebuild-semantic").click()
                await page.wait_for_function(
                    "window.__calls.some(x=>x.method==='POST'&&x.path==='semantic/rebuild')"
                )
                await page.screenshot(path=str(output / "tasks.png"), full_page=True)
                await page.locator('[data-tab="settings"]').click()
                await page.locator('[name="tagging_rpm"]').fill("9")
                await page.locator('[name="embedding_rpm"]').fill("12")
                await page.get_by_role("switch", name="已在 LONG Hub 启用订阅", exact=True).click()
                await page.get_by_role("checkbox", name="允许 violent", exact=True).uncheck()
                await page.get_by_role("combobox", name="视觉标注提供商", exact=True).click()
                await page.get_by_role("option", name="暂不启用视觉标注", exact=True).click()
                await page.locator("#settings-form button[type=submit]").click()
                await page.wait_for_function("window.__lastSaved?.tagging_rpm === 9")
                saved = await page.evaluate("window.__lastSaved")
                assert "app_key" not in saved and "webhook_secret" not in saved
                assert saved["allowed_ratings"] == ["none", "moderate"], saved
                assert saved["auto_tagging"] is True and saved["webhook_ready"] is True, saved
                assert saved["vision_provider_id"] == "", saved
                assert (
                    saved["embedding_provider_id"] == "embedding" and saved["embedding_rpm"] == 12
                ), saved
                await expect(page.locator('[name="tagging_rpm"]')).to_have_value("9")
                await page.screenshot(path=str(output / "settings.png"), full_page=True)
                await (
                    page.locator('[name="embedding_rpm"]')
                    .locator("xpath=ancestor::section")
                    .screenshot(path=str(output / "embedding-settings.png"))
                )
                await page.locator('[data-tab="overview"]').click()
                if await page.get_by_role("button", name="关闭通知", exact=True).count():
                    await page.get_by_role("button", name="关闭通知", exact=True).click()
                await page.evaluate("document.documentElement.dataset.theme='dark'")
                await expect(page.locator("#refresh")).to_have_css(
                    "background-color", "rgb(28, 29, 37)"
                )
                await page.screenshot(path=str(output / "overview-dark.png"), full_page=True)
                await page.evaluate("document.documentElement.dataset.theme='light'")
                await page.set_viewport_size({"width": 390, "height": 844})
                for tab in ["overview", "catalog", "logs", "tasks", "settings"]:
                    await page.locator(f'[data-tab="{tab}"]').click()
                    await expect(page.locator("h1")).to_be_visible()
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    ), tab
                    if tab in ["overview", "catalog", "tasks"]:
                        await page.screenshot(path=str(output / f"{tab}-mobile.png"))
                await page.set_viewport_size({"width": 1440, "height": 1100})
                await page.goto(url + "#/catalog/9223372036854775807")
                await expect(page.locator("#manual-tags")).to_be_visible()
                await page.reload()
                await expect(page.locator("#manual-tags")).to_be_visible()
                await page.keyboard.press("Escape")
                await expect(page).to_have_url(url + "#/catalog")
                await page.goto(origin + "/sandbox.html")
                frame = page.frame_locator("iframe")
                await expect(frame.locator(".meme-card")).to_have_count(30)
                await frame.locator(".card-image").first.click()
                await expect(frame.locator("#manual-tags")).to_be_visible()
                await frame.get_by_role("button", name="关闭", exact=True).click()
                await expect(frame.get_by_role("dialog")).to_have_count(0)
                await frame.locator('[data-tab="settings"]').click()
                await expect(frame.locator("#settings-form")).to_be_visible()
                assert not errors, errors
                print(
                    "SPA checks passed: routes/history/reload, filters/pagination, edits/int64/XSS, selections, retry, logs/tasks/settings, dark/mobile, sandbox iframe"
                )
                print(output / "overview.png")
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(check())
