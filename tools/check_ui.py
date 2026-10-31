"""Edge acceptance checks for the built SPA, using only in-memory bridge/media fixtures."""

import argparse
import asyncio
import base64
import html
import io
import json
from pathlib import Path

from aiohttp import web
from PIL import Image
from playwright.async_api import async_playwright, expect

from backend.models import Settings

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/longhub/views/catalog/_t/test-session/"

BRIDGE = r"""
(() => {
 const settings = SETTINGS;
 settings.app_key_configured = true; settings.webhook_secret_configured = true; settings.webhook_ready=true; settings.embedding_provider_id='embedding';
 window.__settings=settings;
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
  tag_jobs:{queued:12,running:1,done:48,failed:4},tagging_paused:false,tagging_auto:false,ready:true,
  configuration:{app_key_configured:true,webhook_secret_configured:true,webhook_ready:true},
  tag_queue:items.slice(0,5).map((item,i)=>({image_id:item.id,text:item.text,status:i===0?'running':'queued',attempts:0,next_attempt:0})),
  receiver:{started:true,error:null,listen:'127.0.0.1:8791',path:'/hooks/longhub'},
  sync:{status:'done'},last_sync:{completed_at:1792886400},last_webhook:1792886460,vision_configured:true,
  semantic:{configured:true,paused:false,provider_id:'embedding',model:'embedding-demo',total:65,annotated:49,coverage:49/65,indexed:49,pending:0,available:true,state:'ready',error:null,catalog:{total:65,unannotated:16,annotated_unindexed:0,indexed:49}}};
 window.__status=status;
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
   if(path==='settings/save'){
    window.__lastSaved=structuredClone(body);
    const {app_key,webhook_secret,...safe}=body;Object.assign(settings,safe);
    if(app_key!==undefined)settings.app_key_configured=!!app_key.trim();
    if(webhook_secret!==undefined)settings.webhook_secret_configured=!!webhook_secret.trim();
    status.configuration={app_key_configured:settings.app_key_configured,webhook_secret_configured:settings.webhook_secret_configured,webhook_ready:settings.webhook_ready};
    status.ready=settings.app_key_configured&&settings.webhook_secret_configured&&settings.webhook_ready;
    return {saved:true,settings:structuredClone(settings)};
   }
   if(path==='tagging/pause'){status.tagging_paused=body.paused;return {paused:body.paused};}
   if(path==='tagging/auto'){status.tagging_auto=body.enabled;return {enabled:body.enabled};}
   if(path==='semantic/pause'){status.semantic.paused=body.paused;return {paused:body.paused};}
   if(path==='tagging/enqueue')return {queued:body.ids?.length||16};
   if(path==='sync'){
    if(!status.ready||!status.receiver.started)throw new Error('尚未完成同步配置');
    return {id:'test-sync',status:'queued'};
   }
   if(path==='webhook/retry')return {queued:true};
   if(path==='semantic/rebuild'){
    status.semantic.indexed=0;status.semantic.pending=status.semantic.annotated;status.semantic.available=false;
    status.semantic.catalog.annotated_unindexed+=status.semantic.catalog.indexed;status.semantic.catalog.indexed=0;
    return {queued:true};
   }
   throw new Error('Unexpected POST '+path);
  }
 };
})();
"""


async def check(*, maibot=False, sandbox_only=False):
    bridge_script = BRIDGE.replace(
        "SETTINGS", json.dumps(Settings(vision_provider_id="vision").public())
    )
    if maibot:
        preview = io.BytesIO()
        Image.new("RGB", (80, 60), "#ece8f7").save(preview, "JPEG")
        data_url = "data:image/jpeg;base64," + base64.b64encode(preview.getvalue()).decode()
        bridge_script += r"""
(() => {
 const original=window.AstrBotPluginView;
 window.__maibotActive=0;window.__maibotMax=0;
 async function call(operation){
  window.__maibotMax=Math.max(window.__maibotMax,++window.__maibotActive);
  if(window.__maibotActive>2)throw new Error('MaiBot concurrency budget exceeded');
  try{await new Promise(resolve=>setTimeout(resolve,2));return await operation();}
  finally{window.__maibotActive--;}
 }
 window.maibot={ready:Promise.resolve({theme:'light'}),context:{theme:'light'},
  query:(alias,{path})=>call(()=>{
   if(alias!=='read')throw new Error('Invalid query alias');
   const url=new URL(path,'https://bridge.invalid');
   if(url.pathname.endsWith('/preview'))return {data_url:DATA_URL};
   return original.apiGet(url.pathname.slice(1),Object.fromEntries(url.searchParams));
  }),
  action:(alias,{path,payload})=>call(()=>{
   if(alias!=='write')throw new Error('Invalid action alias');
   return original.apiPost(path.slice(1),JSON.parse(payload));
  })
 };
 delete window.AstrBotPluginView;
})();
""".replace("DATA_URL", json.dumps(data_url))
    csp = "default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"

    @web.middleware
    async def cors(request, handler):
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        if maibot and request.path.endswith("index.html"):
            response.headers["Content-Security-Policy"] = csp
        return response

    app = web.Application(middlewares=[cors])
    app.router.add_static(PREFIX, ROOT / "views" / ("maibot" if maibot else "catalog"))

    async def sandbox(_):
        if maibot:
            document = (ROOT / "views" / "maibot" / "index.html").read_text(encoding="utf-8")
            document = document.replace(
                "<head>",
                '<head><meta http-equiv="Content-Security-Policy" content="'
                + html.escape(csp, quote=True)
                + '"><script>'
                + bridge_script
                + "</script>",
            )
            return web.Response(
                text='<iframe title="Plugin View" sandbox="allow-scripts" srcdoc="'
                + html.escape(document, quote=True)
                + '" style="border:0;width:100%;height:950px"></iframe>',
                content_type="text/html",
            )
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
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text) if message.type == "error" else None
                    ),
                )
                await page.add_init_script(bridge_script)
                await page.route("https://r2.longhub.top/**", media)
                if sandbox_only:
                    await page.goto(origin + "/sandbox.html")
                    await page.wait_for_timeout(1500)
                    frame = page.frame_locator("iframe")
                    print("Sandbox errors:", errors, flush=True)
                    print(
                        "Sandbox body:",
                        (await frame.locator("body").inner_text())[:1000],
                        flush=True,
                    )
                    await expect(frame.locator('[data-tab="catalog"]')).to_be_visible(timeout=3000)
                    return
                await page.goto(url)
                await expect(page.locator("#connection-state")).to_have_text("已连接接收器")
                await expect(page.locator(".page-heading h1 + p")).to_have_count(0)
                await expect(page.locator(".page-footer")).to_have_count(0)
                await expect(page.locator(".overview-intro, #metrics")).to_have_count(0)
                await expect(page.get_by_role("link", name="同步协议", exact=True)).to_have_count(0)
                await expect(
                    page.get_by_role("heading", name="目录分布", exact=True)
                ).to_have_count(0)
                await expect(page.locator("#catalog-total")).to_contain_text("65")
                await page.evaluate(
                    "Object.assign(window.__status.sync,{status:'running',phase:'checking_version'})"
                )
                await page.locator("#refresh").click()
                await expect(page.locator("#sync-panel")).to_contain_text("正在检查远端目录版本")
                await expect(
                    page.get_by_role("button", name="校准进行中", exact=True)
                ).to_be_disabled()
                await page.evaluate(
                    "Object.assign(window.__status.sync,{status:'done',skipped:true})"
                )
                await page.locator("#refresh").click()
                await expect(page.locator("#sync-panel")).to_contain_text(
                    "远端目录未变化，已跳过全量同步"
                )
                await page.get_by_role("button", name="立即校准", exact=True).click()
                await page.wait_for_function(
                    "window.__calls.some(x=>x.method==='POST'&&x.path==='sync')"
                )
                await page.evaluate("window.__status.sync={status:'done'}")
                await page.locator("#refresh").click()
                await expect(page.locator("#annotation-chart canvas")).to_have_attribute(
                    "aria-label", "图库标注进度：待标注 16 张，已标注未向量化 0 张，已向量化 49 张"
                )
                await expect(page.locator(".chart-legend li")).to_have_count(3)
                sync_box = await page.locator("#sync-panel").bounding_box()
                total_box = await page.locator("#catalog-total").bounding_box()
                chart_box = await page.locator("#annotation-chart").bounding_box()
                hook_box = await page.locator("#webhook-panel").bounding_box()
                assert sync_box["y"] == total_box["y"] and sync_box["x"] < total_box["x"]
                assert chart_box["y"] == hook_box["y"] and chart_box["x"] < hook_box["x"]
                assert 2.9 < sync_box["width"] / total_box["width"] < 3.1
                assert 1.9 < hook_box["width"] / chart_box["width"] < 2.1
                await expect(page.locator(".webhook-actions")).to_have_css(
                    "border-top-width", "0px"
                )
                assert (
                    await page.evaluate(
                        "getComputedStyle(document.documentElement).backgroundColor"
                    )
                    == "rgb(242, 243, 245)"
                )
                ready_icon = page.get_by_role("button", name="语义查询可用", exact=True)
                await ready_icon.hover()
                await expect(page.get_by_role("tooltip")).to_contain_text(
                    "上下文明确相关时优先使用"
                )
                await page.keyboard.press("Escape")
                await page.locator("#refresh").focus()
                # Observe real rendering across incremental updates, loss of readiness and empty catalogs.
                await page.evaluate("""() => {
                    window.__originalStatus=structuredClone(window.__status);
                    Object.assign(window.__status.semantic, {available:false,indexed:43,pending:6,state:'indexing'});
                    Object.assign(window.__status.semantic.catalog, {annotated_unindexed:6,indexed:43});
                }""")
                await page.locator("#refresh").click()
                await expect(ready_icon).to_have_count(0)
                await expect(page.locator("#annotation-chart canvas")).to_have_attribute(
                    "aria-label", "图库标注进度：待标注 16 张，已标注未向量化 6 张，已向量化 43 张"
                )
                await page.evaluate("""() => {
                    window.__status.total=0;
                    Object.assign(window.__status.semantic.catalog,{total:0,unannotated:0,annotated_unindexed:0,indexed:0});
                }""")
                await page.locator("#refresh").click()
                await expect(page.locator("#annotation-chart")).to_contain_text("暂无表情")
                await page.evaluate("Object.assign(window.__status, window.__originalStatus)")
                await page.locator("#refresh").click()
                await expect(ready_icon).to_be_visible()
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
                await page.locator('[data-tab="logs"]').click()
                await expect(page.locator("#log-rows tr")).to_have_count(3)
                await page.get_by_role("button", name="查看日志 1", exact=True).click()
                await expect(page.locator("#log-detail")).to_contain_text("9223372036854775807")
                await page.keyboard.press("Escape")
                await page.get_by_role("checkbox", name="只看失败", exact=True).check()
                await expect(page.locator("#log-rows tr")).to_have_count(2)
                await page.locator('[data-tab="tasks"]').click()
                await page.evaluate("""() => {
                    window.__taskSemantic=structuredClone(window.__status.semantic);
                    Object.assign(window.__status.semantic,{indexed:43,pending:6,available:false,state:'indexing'});
                    Object.assign(window.__status.semantic.catalog,{annotated_unindexed:6,indexed:43});
                }""")
                await page.locator("#refresh").click()
                await expect(page.locator(".tasks-layout > section")).to_have_count(2)
                await expect(
                    page.locator(".metrics-grid, .task-callout, .source-cards")
                ).to_have_count(0)
                await expect(
                    page.get_by_role("list", name="标注队列前五项").locator("li")
                ).to_have_count(5)
                await expect(
                    page.get_by_role("link", name="查看标注任务 9223372036854775807", exact=True)
                ).to_be_visible()
                await expect(page.locator("#embedding-progress canvas")).to_be_visible()
                tagging_box = await page.locator("#tagging-progress").bounding_box()
                embedding_box = await page.locator("#embedding-progress").bounding_box()
                assert tagging_box["y"] == embedding_box["y"]
                assert 1.9 < tagging_box["width"] / embedding_box["width"] < 2.1
                summary_box = await page.locator(".annotation-overview").bounding_box()
                queue_box = await page.locator(".tagging-queue").bounding_box()
                assert 2.9 < summary_box["width"] / queue_box["width"] < 3.1
                await expect(page.locator("#embedding-progress canvas")).to_have_attribute(
                    "aria-label", "Embedding 进度：待向量化 6 张，已向量化 43 张"
                )
                await page.locator("#refresh").focus()
                await page.get_by_role("switch", name="自动标注", exact=True).click()
                await expect(
                    page.get_by_role("switch", name="自动标注", exact=True)
                ).to_be_checked()
                await page.locator("#pause-tagging").click()
                await expect(page.locator("#pause-tagging")).to_have_attribute(
                    "aria-label", "继续标注队列"
                )
                await expect(
                    page.get_by_role("progressbar", name="图库标注进度")
                ).to_have_attribute("aria-valuenow", "75")
                await page.locator("#pause-embedding").hover()
                await expect(page.get_by_role("tooltip")).to_contain_text("暂停新的后台向量化任务")
                await page.locator("#pause-embedding").click()
                await expect(page.locator("#pause-embedding")).to_have_attribute(
                    "aria-label", "继续 Embedding"
                )
                await page.locator("#tag-missing").click()
                await page.wait_for_function(
                    "window.__calls.some(x=>x.path==='tagging/enqueue'&&x.body.force===true)"
                )
                await page.locator("#rebuild-semantic").click()
                await page.wait_for_function(
                    "window.__calls.some(x=>x.method==='POST'&&x.path==='semantic/rebuild')"
                )
                await expect(page.locator("#embedding-progress canvas")).to_have_attribute(
                    "aria-label", "Embedding 进度：待向量化 49 张，已向量化 0 张"
                )
                await page.locator("#refresh").focus()
                await page.locator("#pause-embedding").click()
                await expect(page.locator("#pause-embedding")).to_have_attribute(
                    "aria-label", "暂停 Embedding"
                )
                await page.locator("#pause-tagging").click()
                await page.get_by_role("switch", name="自动标注", exact=True).click()
                await expect(
                    page.get_by_role("switch", name="自动标注", exact=True)
                ).not_to_be_checked()
                await page.evaluate(
                    "Object.assign(window.__status.semantic, window.__taskSemantic)"
                )
                await page.locator("#refresh").click()
                await page.locator('[data-tab="settings"]').click()
                await page.locator('[name="tagging_rpm"]').fill("9")
                await page.locator('[name="embedding_rpm"]').fill("12")
                subscription = page.get_by_role("switch", name="已在 LONG Hub 启用订阅", exact=True)
                if not await subscription.is_checked():
                    await subscription.click()
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
                await page.locator('[data-tab="overview"]').click()
                if await page.get_by_role("button", name="关闭通知", exact=True).count():
                    await page.get_by_role("button", name="关闭通知", exact=True).click()
                await page.evaluate("document.documentElement.dataset.theme='dark'")
                await expect(page.locator("#refresh")).to_have_css(
                    "background-color", "rgb(28, 29, 37)"
                )
                await page.locator('[data-tab="tasks"]').click()
                await expect(page.locator("#embedding-progress canvas")).to_be_visible()
                await page.evaluate("document.documentElement.dataset.theme='light'")
                await page.set_viewport_size({"width": 390, "height": 844})
                for tab in ["overview", "catalog", "logs", "tasks", "settings"]:
                    await page.locator(f'[data-tab="{tab}"]').click()
                    await expect(page.locator("h1")).to_be_visible()
                    await expect(page.locator(".page-heading h1 + p")).to_have_count(0)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    ), tab
                await page.set_viewport_size({"width": 1440, "height": 1100})
                await page.goto(url + "#/catalog/9223372036854775807")
                await expect(page.locator("#manual-tags")).to_be_visible()
                await page.reload()
                await expect(page.locator("#manual-tags")).to_be_visible()
                await page.keyboard.press("Escape")
                await expect(page).to_have_url(url + "#/catalog")
                await page.goto(origin + "/sandbox.html")
                frame = page.frame_locator("iframe")
                if maibot:
                    await page.wait_for_timeout(500)
                    if not await frame.locator('[data-tab="catalog"]').count():
                        print("Sandbox errors:", errors)
                        print("Sandbox body:", await frame.locator("body").inner_text())
                    await frame.locator('[data-tab="catalog"]').click()
                await expect(frame.locator(".meme-card")).to_have_count(30)
                if maibot:
                    await expect(frame.locator(".card-image img").first).to_be_visible()
                    assert (
                        await page.locator("iframe").evaluate("el=>el.sandbox.value")
                        == "allow-scripts"
                    )
                    assert await page.frames[1].evaluate("window.__maibotMax") <= 2
                await frame.locator(".card-image").first.click()
                await expect(frame.locator("#manual-tags")).to_be_visible()
                if maibot:
                    await frame.locator("#manual-tags").fill("沙箱标签")
                    await frame.get_by_role("button", name="保存修订", exact=True).click()
                else:
                    await frame.get_by_role("button", name="关闭", exact=True).click()
                await expect(frame.get_by_role("dialog")).to_have_count(0)
                if maibot:
                    await frame.locator("#query").fill("摸鱼")
                    await frame.locator('#search-form button[type="submit"]').click()
                    await expect(frame.locator(".meme-card")).to_have_count(8)
                    await frame.locator("#query").fill("")
                    await frame.locator("#query").press("Enter")
                    await expect(frame.locator(".meme-card")).to_have_count(30)
                await frame.locator('[data-tab="settings"]').click()
                await expect(frame.locator("#settings-form")).to_be_visible()
                if maibot:
                    await frame.locator('[name="tagging_rpm"]').fill("11")
                    await frame.locator('#settings-form button[type="submit"]').click()
                    await expect(frame.locator(".notice")).to_contain_text("配置已保存")
                    assert (await page.frames[1].evaluate("window.__lastSaved"))[
                        "tagging_rpm"
                    ] == 11
                    await expect(frame.locator('[name="tagging_rpm"]')).to_have_value("11")
                    await frame.locator('[data-tab="tasks"]').click()
                    await frame.locator("#pause-tagging").click()
                    await expect(frame.locator("#pause-tagging")).to_have_attribute(
                        "aria-label", "继续标注队列"
                    )
                await frame.locator('[data-tab="overview"]').click()
                await expect(frame.locator("#annotation-chart canvas")).to_be_visible()
                await frame.get_by_role("button", name="语义查询可用", exact=True).hover()
                await expect(frame.get_by_role("tooltip")).to_contain_text(
                    "上下文明确相关时优先使用"
                )
                # First-time setup must identify the missing subscription confirmation,
                # retain both secrets during its sparse save, and reach a real sync call.
                await page.frames[1].evaluate("""() => {
                    Object.assign(window.__settings,{app_key_configured:false,webhook_secret_configured:false,webhook_ready:false});
                    Object.assign(window.__status,{ready:false,configuration:{app_key_configured:false,webhook_secret_configured:false,webhook_ready:false}});
                }""")
                await frame.locator("#refresh").click()
                await expect(frame.locator("#sync-panel")).to_contain_text(
                    "Appkey、Webhook 签名密钥"
                )
                await expect(
                    frame.get_by_role("button", name="立即校准", exact=True)
                ).to_be_disabled()
                await frame.locator('[data-tab="settings"]').click()
                await frame.locator('[name="app_key"]').fill("test-app-key")
                await frame.locator('[name="webhook_secret"]').fill("test-hook-secret")
                await expect(
                    frame.get_by_role("switch", name="已在 LONG Hub 启用订阅", exact=True)
                ).not_to_be_checked()
                await frame.locator('#settings-form button[type="submit"]').click()
                await expect(frame.locator('[name="app_key"]')).to_have_value("")
                assert (await page.frames[1].evaluate("window.__lastSaved"))[
                    "app_key"
                ] == "test-app-key"
                await frame.locator('[data-tab="overview"]').click()
                await expect(frame.locator("#connection-state")).to_have_text("待确认订阅")
                await expect(frame.locator("#sync-panel")).to_contain_text("密钥已保存")
                await frame.get_by_role("button", name="确认订阅并校准", exact=True).click()
                await expect(frame.locator("#connection-state")).to_have_text("已连接接收器")
                assert await page.frames[1].evaluate(
                    "window.__calls.some(x=>x.method==='POST'&&x.path==='sync') && window.__status.ready"
                )
                assert await page.frames[1].evaluate("window.__lastSaved") == {
                    "webhook_ready": True
                }
                await expect(frame.locator("#connection-state")).to_have_text("已连接接收器")
                await expect(
                    frame.get_by_role("button", name="立即校准", exact=True)
                ).to_be_enabled()
                assert not errors, errors
                print(
                    "SPA checks passed: overview layout/pie/tooltip/updates/empty, routes/history/reload, filters/pagination, edits/int64/XSS, selections, retry, logs/tasks/settings, dark/mobile, sandbox iframe"
                )
            finally:
                await browser.close()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--maibot", action="store_true")
    parser.add_argument("--sandbox-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(check(maibot=args.maibot, sandbox_only=args.sandbox_only))
