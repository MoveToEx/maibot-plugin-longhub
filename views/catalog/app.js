const $ = (selector) => document.querySelector(selector);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const state = {tab: "overview", page: 1, logPage: 1, selected: new Set(), status: null, current: null, busy: false};
const titles = {overview: "运行概览", catalog: "表情目录", logs: "Webhook 日志", tasks: "标注任务", settings: "连接与设置"};
const numericSettings = ["webhook_port", "sync_interval_hours", "tagging_rpm", "tagging_concurrency", "candidate_limit", "max_image_mb", "webhook_log_days", "webhook_log_limit"];
const boolSettings = ["webhook_ready", "auto_tagging"];
const date = (value) => value ? new Date(value * 1000).toLocaleString("zh-CN", {hour12: false}) : "尚无记录";
const statuses = {idle: "空闲", queued: "排队中", running: "处理中", done: "已完成", failed: "失败", cancelled: "已取消"};

function node(tag, className = "", text = null) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== null) el.textContent = text;
  return el;
}
function notice(message, error = false) {
  $("#notice").hidden = false;
  $("#notice").className = "notice" + (error ? " error" : "");
  $("#notice").textContent = message;
}
async function action(fn) {
  if (state.busy) return;
  state.busy = true;
  try { await fn(); } catch (error) { notice(error.message || String(error), true); }
  finally { state.busy = false; }
}
function metric(label, value, description) {
  const el = node("div", "metric");
  el.append(node("div", "metric-label", label), node("div", "metric-value", String(value)), node("small", "", description));
  return el;
}
function details(target, pairs) {
  target.replaceChildren();
  for (const [key, value] of pairs) {
    const row = node("div", "detail-line");
    row.append(node("span", "", key), node("span", "", String(value)));
    target.append(row);
  }
}
function badge(el, text, good = false, bad = false) {
  el.textContent = text;
  el.className = "badge" + (good ? " good" : "") + (bad ? " bad" : "");
}
async function refreshStatus() {
  const s = await bridge.apiGet("status");
  state.status = s;
  $("#connection-dot").classList.toggle("online", s.receiver.started && s.ready);
  $("#connection-state").textContent = s.ready ? (s.receiver.started ? "已连接接收器" : "接收器异常") : "等待首次配置";
  const jobs = s.tag_jobs || {};
  $("#metrics").replaceChildren(
    metric("本地表情目录", s.total.toLocaleString(), "完整元数据，远程图片"),
    metric("等待刷新通知", s.pending_notifications.toLocaleString(), "已持久化的 webhook 通知"),
    metric("待标注任务", (jobs.queued || 0) + (jobs.running || 0), "按需与手工排队"),
    metric("已完成标注", jobs.done || 0, "保留人工修订"),
  );
  const sync = s.sync;
  badge($("#sync-badge"), !s.ready ? "等待配置" : statuses[sync.status] || sync.status, sync.status === "done", sync.status === "failed");
  details($("#sync-details"), [
    ["上次完整校准", date(s.last_sync?.completed_at)],
    ["当前导入进度", sync.status === "running" ? `${sync.imported || 0} 条 / 已刷新 ${sync.refreshed || 0} 张` : "—"],
    ["最近错误", sync.error || s.sync_error || "无"],
  ]);
  badge($("#receiver-badge"), s.receiver.started ? "正在监听" : "未启动", s.receiver.started, !s.receiver.started);
  details($("#receiver-details"), [
    ["本地监听", s.receiver.listen], ["接收路径", s.receiver.path],
    ["最近有效调用", date(s.last_webhook)], ["运行状态", s.receiver.error || s.background_error || "正常"],
  ]);
  $("#task-metrics").replaceChildren(
    metric("排队中", jobs.queued || 0, "等待模型调用"), metric("处理中", jobs.running || 0, "在途任务"),
    metric("已完成", jobs.done || 0, "生成有效描述与标签"), metric("待重试", jobs.failed || 0, "可在目录详情中重新标注"),
  );
  badge($("#tag-state"), !s.vision_configured ? "尚未选择模型" : s.tagging_paused ? "队列已暂停" : "队列运行中", s.vision_configured && !s.tagging_paused);
  $("#pause-tagging").textContent = s.tagging_paused ? "继续队列" : "暂停队列";
}
function safeImage(img, url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error();
    img.src = parsed.href;
  } catch { img.alt = "图片地址无效"; }
  img.referrerPolicy = "no-referrer";
  img.loading = "lazy";
  img.addEventListener("error", () => { img.alt = "远程图片暂时不可用"; }, {once: true});
}
function updateSelected() { $("#selected-count").textContent = state.selected.size; }
function pagination(target, page, total, onPage) {
  const pages = Math.max(1, Math.ceil(total / 30));
  const prev = node("button", "button secondary", "← 上一页");
  const next = node("button", "button secondary", "下一页 →");
  prev.disabled = page <= 1; next.disabled = page >= pages;
  prev.addEventListener("click", () => action(() => onPage(page - 1)));
  next.addEventListener("click", () => action(() => onPage(page + 1)));
  target.replaceChildren(prev, node("span", "", `${page} / ${pages}`), next);
}
async function loadCatalog() {
  const result = await bridge.apiGet("memes", {q: $("#query").value, page: state.page, limit: 30,
    rating: $("#rating-filter").value, annotation: $("#annotation-filter").value, enabled: $("#enabled-filter").value});
  $("#catalog-count").textContent = `共 ${result.total.toLocaleString()} 张表情 · 点击图片编辑标签`;
  const cards = result.items.map((item) => {
    const card = node("article", "meme-card" + (!item.enabled ? " disabled" : ""));
    const label = node("label", "card-check");
    const check = node("input"); check.type = "checkbox"; check.checked = state.selected.has(item.id);
    check.setAttribute("aria-label", `选择表情 ${item.id}`);
    check.addEventListener("change", () => {
      if (check.checked) state.selected.add(item.id); else state.selected.delete(item.id);
      updateSelected();
    });
    label.append(check);
    const button = node("button", "card-image");
    button.setAttribute("aria-label", `查看表情 ${item.id}`);
    const img = node("img"); img.alt = item.text || `表情 ${item.id}`; safeImage(img, item.image_url);
    button.append(img); button.addEventListener("click", () => action(() => openMeme(item.id)));
    const body = node("div", "card-body");
    const title = node("strong", "card-title", item.text || "无文字表情"); title.title = item.text;
    const chips = node("div");
    item.effective_tags.slice(0, 3).forEach((tag) => chips.append(node("span", "chip", tag)));
    const meta = node("div", "card-meta");
    meta.append(node("span", "", `#${item.id}`), node("span", "badge", !item.enabled ? "已禁用" : item.annotation_fresh ? "已标注" : item.rating));
    body.append(title, chips, meta); card.append(button, label, body); return card;
  });
  $("#gallery").replaceChildren(...(cards.length ? cards : [node("div", "empty", result.total ? "本页没有条目，请返回上一页。" : "暂时没有匹配的表情。首次使用请先完成连接与同步。") ]));
  pagination($("#catalog-pagination"), state.page, result.total, async (page) => { state.page = page; await loadCatalog(); });
}
async function openMeme(id) {
  const item = await bridge.apiGet(`memes/${id}`);
  state.current = item;
  $("#meme-title").textContent = `表情 #${item.id}`;
  $("#detail-caption").textContent = item.text || "无文字";
  $("#detail-rating").textContent = item.rating;
  safeImage($("#detail-image"), item.image_url);
  $("#detail-sources").replaceChildren();
  for (const [title, key, cls] of [["Hub", "hub_tags", "source-hub"], ["视觉模型", "ai_tags", "source-ai"], ["人工", "manual_tags", "source-manual"]]) {
    const group = node("div", "source-group"); group.append(node("small", "", title));
    if (!item[key].length) group.append(node("span", "muted small", "尚无标签"));
    item[key].forEach((value) => {
      const chip = node("button", `chip ${cls}`, value); chip.type = "button";
      chip.title = "点击添加至屏蔽列表";
      chip.addEventListener("click", () => { $("#excluded-tags").value = [...new Set([...splitTags($("#excluded-tags").value), value])].join(", "); });
      group.append(chip);
    });
    $("#detail-sources").append(group);
  }
  $("#manual-tags").value = item.manual_tags.join(", ");
  $("#excluded-tags").value = item.excluded_tags.join(", ");
  $("#manual-description").value = item.manual_description ?? item.ai_description;
  $("#use-ai-description").checked = item.manual_description === null;
  $("#manual-description").disabled = item.manual_description === null;
  $("#meme-enabled").checked = item.enabled;
  $("#detail-job").textContent = item.tag_job ? `标注：${statuses[item.tag_job.status] || item.tag_job.status}${item.tag_job.error ? " · " + item.tag_job.error : ""}` : "尚未进行视觉标注";
  if (!$("#meme-dialog").open) $("#meme-dialog").showModal();
}
const splitTags = (value) => [...new Set(value.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean))];
async function loadLogs() {
  const result = await bridge.apiGet("webhook/logs", {page: state.logPage, limit: 30,
    event: $("#event-filter").value, failed: String($("#failed-filter").checked)});
  $("#logs-empty").hidden = result.items.length > 0;
  $("#log-rows").replaceChildren(...result.items.map((log) => {
    const row = node("tr");
    const processed = log.http_status !== 204 ? "未接受" : log.processed ? "已更新" : log.processing_error ? `待重试 · ${log.attempts} 次` : "等待刷新";
    [date(log.received), `${log.event || "—"} / #${log.image_id || "—"}`, log.signature_valid ? "有效" : "失败", String(log.http_status), processed].forEach((v) => row.append(node("td", "", v)));
    const cell = node("td"); const button = node("button", "button text-button", "详情");
    button.addEventListener("click", () => {
      $("#log-detail").textContent = JSON.stringify(log, null, 2); $("#log-dialog").showModal();
    });
    cell.append(button); row.append(cell); return row;
  }));
  pagination($("#log-pagination"), state.logPage, result.total, async (page) => { state.logPage = page; await loadLogs(); });
}
async function loadSettings() {
  const result = await bridge.apiGet("settings"); const s = result.settings;
  const form = $("#settings-form");
  const providers = form.elements.vision_provider_id;
  providers.replaceChildren(new Option("暂不启用视觉标注", ""), ...result.providers.map((p) => new Option(p.name, p.id)));
  if (s.vision_provider_id && !result.providers.some((p) => p.id === s.vision_provider_id)) providers.add(new Option(`${s.vision_provider_id}（当前不可用）`, s.vision_provider_id));
  for (const [key, value] of Object.entries(s)) {
    const input = form.elements.namedItem(key);
    if (!input || ["app_key", "webhook_secret", "allowed_ratings"].includes(key)) continue;
    if (boolSettings.includes(key)) input.checked = value;
    else input.value = Array.isArray(value) ? value.join(", ") : value;
  }
  for (const rating of ["none", "moderate", "violent"]) form.elements[`rating_${rating}`].checked = s.allowed_ratings.includes(rating);
  form.elements.app_key.value = ""; form.elements.webhook_secret.value = "";
  $("#app-key-state").textContent = s.app_key_configured ? "已配置，留空保留" : "尚未配置";
  $("#secret-state").textContent = s.webhook_secret_configured ? "已配置，留空保留" : "尚未配置";
}
async function switchTab(tab) {
  state.tab = tab;
  for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== tab;
  for (const button of document.querySelectorAll(".nav")) button.classList.toggle("active", button.dataset.tab === tab);
  $("#page-title").textContent = titles[tab];
  if (tab === "catalog") await loadCatalog();
  else if (tab === "logs") await loadLogs();
  else if (tab === "settings") await loadSettings();
  else await refreshStatus();
}
document.querySelectorAll("[data-tab], [data-open-tab]").forEach((button) => button.addEventListener("click", () => action(() => switchTab(button.dataset.tab || button.dataset.openTab))));
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.close}`).close()));
$("#refresh").addEventListener("click", () => action(async () => { await refreshStatus(); if (["catalog", "logs"].includes(state.tab)) await switchTab(state.tab); }));
$("#search-form").addEventListener("submit", (event) => { event.preventDefault(); action(async () => { state.page = 1; await loadCatalog(); }); });
for (const selector of ["#rating-filter", "#annotation-filter", "#enabled-filter"]) $(selector).addEventListener("change", () => action(async () => { state.page = 1; await loadCatalog(); }));
for (const selector of ["#event-filter", "#failed-filter"]) $(selector).addEventListener("change", () => action(async () => { state.logPage = 1; await loadLogs(); }));
$("#use-ai-description").addEventListener("change", () => { $("#manual-description").disabled = $("#use-ai-description").checked; });
$("#edit-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(async () => {
    await bridge.apiPost(`memes/${state.current.id}/edit`, {manual_tags: splitTags($("#manual-tags").value), excluded_tags: splitTags($("#excluded-tags").value),
      description: $("#use-ai-description").checked ? null : $("#manual-description").value, enabled: $("#meme-enabled").checked});
    $("#meme-dialog").close(); notice("人工修订已保存，后续同步会保留。"); await loadCatalog();
  });
});
document.querySelectorAll('[data-action="sync"]').forEach((button) => button.addEventListener("click", () => action(async () => {
  const result = await bridge.apiPost("sync", {}); notice(`全量校准已排队 · ${result.id || "进行中"}`); await refreshStatus();
})));
$("#tag-selected").addEventListener("click", () => action(async () => {
  if (!state.selected.size) { notice("请先勾选需要标注的表情。"); return; }
  const result = await bridge.apiPost("tagging/enqueue", {ids: [...state.selected]});
  notice(`已加入 ${result.queued} 个标注任务。`); state.selected.clear(); updateSelected(); await loadCatalog();
}));
$("#tag-one").addEventListener("click", () => action(async () => {
  const result = await bridge.apiPost("tagging/enqueue", {ids: [state.current.id], force: true});
  $("#detail-job").textContent = result.queued ? "已重新加入标注队列" : "该条目已经在队列中";
}));
$("#tag-missing").addEventListener("click", () => action(async () => {
  const result = await bridge.apiPost("tagging/enqueue", {}); notice(`已加入 ${result.queued} 个标注任务。`); await refreshStatus();
}));
$("#pause-tagging").addEventListener("click", () => action(async () => {
  await bridge.apiPost("tagging/pause", {paused: !state.status.tagging_paused}); await refreshStatus();
}));
$("#retry-webhooks").addEventListener("click", () => action(async () => {
  await bridge.apiPost("webhook/retry", {}); notice("已将待处理通知设为立即重试。"); await loadLogs();
}));
$("#copy-template").addEventListener("click", () => action(async () => {
  try { await navigator.clipboard.writeText($("#webhook-template").textContent); notice("请求体模板已复制。"); }
  catch { notice("浏览器未允许复制，请选择模板文字后手动复制。"); }
}));
$("#settings-form").addEventListener("submit", (event) => {
  event.preventDefault(); action(async () => {
    const form = event.target; const payload = {};
    for (const input of form.elements) {
      if (!input.name || input.name.startsWith("rating_")) continue;
      if (["app_key", "webhook_secret"].includes(input.name) && !input.value.trim()) continue;
      if (numericSettings.includes(input.name)) payload[input.name] = Number(input.value);
      else if (boolSettings.includes(input.name)) payload[input.name] = input.checked;
      else if (input.name === "image_hosts") payload[input.name] = splitTags(input.value);
      else payload[input.name] = input.value.trim();
    }
    payload.allowed_ratings = ["none", "moderate", "violent"].filter((r) => form.elements[`rating_${r}`].checked);
    await bridge.apiPost("settings/save", payload); notice("配置已保存，后台服务已重新启动。"); await loadSettings(); await refreshStatus();
  });
});

if (!bridge) {
  notice("请从 AstrBot 插件详情中的「页面」打开本管理页。", true);
} else {
  await action(async () => { await bridge.ready(); await refreshStatus(); });
  setInterval(async () => {
    if (document.hidden || state.busy || !["overview", "tasks"].includes(state.tab)) return;
    try { await refreshStatus(); } catch { /* Explicit refresh reports actionable errors. */ }
  }, 5000);
}
