# LONG Hub 表情目录 · AstrBot

维护与 [LONG Hub](https://longhub.top) 同步的本地元数据目录，在聊天时向模型提供表情候选及搜索、发送工具。图片始终来自远程 R2，不建立本地图库。

## 运行要求

- AstrBot **4.29.3 或以上**、Python **3.12 或以上**，SQLite 需要 FTS5。
- 首版发送路径以 **OneBot v11／NapCat** 验收；其他适配器尚未联调。
- 一个有效的 LONG Hub Appkey，以及可以被公网 HTTPS 访问的 webhook 回调地址。
- 可选：AstrBot 中已配置的原生视觉模型。首版以 OpenAI 兼容提供商验收，文字模型不适用于标注。

插件本身只持久化 SQLite 目录、人工修订、模型标注、任务和日志。R2 图片在需要时读入内存；调用 AstrBot 标准发送和视觉接口时，框架可能生成并清理临时图片。不执行图片上传或上游元数据修改。

## 安装与首次配置

1. 将本项目放入 AstrBot 的 `data/plugins/astrbot_plugin_longhub/`，或在插件管理中上传打包后的 ZIP。运行环境安装 `requirements.txt` 中的依赖，重载插件。
2. 在插件详情 → 页面打开 **LONG Hub 表情目录**，进入「连接与设置」。填写 Appkey 和自选的 webhook 签名密钥。先保持「已在 LONG Hub 启用订阅」未勾选并保存，接收器即会启动。
3. 将公网地址反向代理至接收器的 `/hooks/longhub`。默认监听 `127.0.0.1:8791`。
4. 在 LONG Hub 创建 webhook，回调 URL 为 `https://你的域名/hooks/longhub`，secret 与插件一致，订阅全部事件（`eventTypes = 7`），请求体模板为：

   ```text
   {"event":{{json .Event}},"imageId":{{json .Data.ImageID}}}
   ```

5. 启用 Hub 订阅后，回到插件页面勾选「已在 LONG Hub 启用订阅」并保存，首次全量同步会自动开始。概览中可查看导入、校验及补齐通知的进度。
6. 需要视觉标签时，选择已配置的 AstrBot 视觉提供商。在目录中勾选条目进行标注，或在「标注任务」为全部未标注图片排队。默认不会自动全库调用模型。

不要把 LONG Hub Appkey 填为 webhook secret，也不要给回调 URL 配置额外的浏览器登录。管理接口由 AstrBot 自身鉴权，独立接收端口只提供 HMAC 校验的 webhook 与无敏感信息的 `/health`。

### Nginx 反向代理

```nginx
location = /hooks/longhub {
    proxy_pass http://127.0.0.1:8791/hooks/longhub;
    proxy_set_header Host $host;
    proxy_set_header X-Signature $http_x_signature;
    proxy_read_timeout 30s;
    client_max_body_size 256k;
}
```

LONG Hub 不接受私网回调地址，也不跟随重定向。请直接填写最终的公网 HTTPS 地址，避免 HTTP → HTTPS 或路径补斜杠重定向。管理页无需额外映射端口。

Docker 部署时将 `webhook_host` 改成 `0.0.0.0`，并给现有 AstrBot 服务添加端口映射，例如 `127.0.0.1:8791:8791`，然后由宿主机反代。若反代也在容器网络中，可直接指向 AstrBot 服务的 8791 端口。

## 聊天与人工管理

插件在 LLM 请求时按当前消息检索最多五个候选，作为临时系统提示附加，不修改人格。模型可调用：

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `search_longhub_memes` | `query: string`, `limit: number = 5` | 按文字、描述和标签搜索，返回字符串 ID、文字、分级、描述及标签 |
| `send_longhub_meme` | `image_id: string`, `position: string = "before"` | 模型选择 `before`（文字前）或 `after`（文字后）发送；单个入站事件最多发送一张 |

聊天模型需要支持工具调用，并在 AstrBot 中启用这两个插件工具。候选只提供参考，没有合适的表情时模型可以不发送。所有分级默认开放，可在设置中限制；本地禁用和分级限制同时应用于搜索与发送。

模型根据语境自主填写 `position`：`before` 会先发送龙图，再继续回复正文；`after` 会先排队，等本轮正文的全部分段或流式输出完成后再发送。旧调用省略该参数时仍默认为 `before`。排队的图片也计入每轮一张的限制，只需要表情时可不附加文字；回复被取消或模型生成失败时不补发排队图片。

检索使用 CJK 双字切分和英文词切分，配合 SQLite FTS5/BM25；模型可以用同义词重新搜索。近期使用的表情降低优先级，不依赖额外向量服务。

在目录中点击图片即可查看 Hub／AI／人工标签来源，添加人工标签、屏蔽错误标签、编写人工描述，或禁止模型使用该条目。有效标签为三类来源合并后扣除屏蔽列表；人工描述优先于 AI 描述。上游 `text` 和 `rating` 只读。人工修订独立保存，即使上游条目删除、以后恢复，也能保留本地设置。

## 同步、恢复与日志

- 全量同步使用 `GET /sync/full` 和 `Authorization: Key ...`，验证完整 NDJSON、SHA-256、记录数、导出 ID、EOF 及 gzip 完整性后才发布新代次。新快照替换旧目录，能清除上游已不存在的条目。
- 下载期间 webhook 继续写入持久化队列。切换目录前重新读取已接收通知涉及的图片，事务性发布新代次；读取期间的新通知留待下一次刷新。
- `create`、`update`、`delete`、`restore` 都只触发 `GET /image/{id}`。不直接应用事件中的旧内容；404、非空 deletion 或空 currentVersionId 才移出有效目录。请求失败不会被当作删除。
- 每次插件启动、每隔 24 小时（可配置）以及手动操作时进行全量校准。校准失败保留旧目录并重试；凭据无效时等待管理员修改配置。
- LONG Hub webhook 是尽力投递，没有增量回放。连续三次调用失败会暂停订阅；修复网络或接收器后，需要在 Hub **手动重新激活**，并点击本插件的「立即全量校准」。仅凭长时间没有 webhook 无法可靠判断订阅是否暂停。
- 日志默认保留 30 天、最多一万条。未完成通知独立于日志保留，失败后退避重试，可手动重试。无效签名的请求体不保存，签名及 Appkey 不进入管理 API 返回值。

数据库位于 AstrBot 的 `data/plugin_data/longhub/catalog.sqlite3`（具体根目录遵循 AstrBot 配置）。停用插件后备份整个 `longhub` 数据目录；不要在运行中只复制主数据库而遗漏 WAL。切换 API 来源前应另行备份本地人工修订。

## 标注队列

默认并发 1、每分钟最多 6 次模型请求，全局限速。候选缺少标注时可自动加入低速队列；手动批量支持暂停和继续。失败三次后标记为失败，进入目录详情点「重新标注」可重试。

标注使用真实图像像素和已确认的上游文字，不把 Hub 标签当作视觉事实。静态图最长边缩至 1024，GIF 在内存中抽取最多四帧组成预览。发送时使用原始图片，保留 GIF 动画。上游文字或图片标识变化时标注会显示过期，可重新排队；生成期间发生变化的结果不会覆盖当前内容。

## 开发、检查与打包

依赖由 `pyproject.toml` 与 `uv.lock` 管理。`requirements.txt` 为 AstrBot 插件安装器保留运行依赖入口。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.uv-python'
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run --group browser python -m tools.check_ui
uv run python tools/package.py
```

测试使用本机临时 HTTP 服务和可控的模型／AstrBot 接口替身，不需要真实 Appkey。覆盖流校验、切换竞态、延迟通知、持久化恢复、标签保护、模型任务和聊天工具边界。可选的浏览器检查使用本机 Microsoft Edge 和内存中的桥接数据，检查页面交互、深色主题与手机布局；不请求远程图片。部署时仍需用真实 Appkey、公网 webhook、视觉提供商和 NapCat 完成联调。

协议来源：[LONG Hub Catalog Sync](https://docs.longhub.top/catalog-sync.html)、[Webhook](https://docs.longhub.top/webhook.html)、[AstrBot Plugin Views](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html)。参考 [smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) 和 [meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager) 的集成与管理交互，本插件独立实现。
