# LONG Hub 表情目录 · AstrBot

维护与 [LONG Hub](https://longhub.top) 同步的本地元数据目录，在聊天时向模型提供表情候选及搜索、发送工具。图片始终来自远程 R2，不建立本地图库。

## 运行要求

- AstrBot **4.29.3 或以上**、Python **3.12 或以上**，SQLite 需要 FTS5。
- 首版发送路径以 **OneBot v11／NapCat** 验收；其他适配器尚未联调。
- 一个有效的 LONG Hub Appkey，以及可以被公网 HTTPS 访问的 webhook 回调地址。
- 可选：AstrBot 中已配置的原生视觉模型。首版以 OpenAI 兼容提供商验收，文字模型不适用于标注。

插件本身只持久化 SQLite 目录、人工修订、模型标注、任务和日志。R2 图片在需要时读入内存；调用 AstrBot 标准发送和视觉接口时，框架可能生成并清理临时图片。不执行图片上传或上游元数据修改。

管理面板为 React + Vite + React Router + Tailwind CSS SPA，使用 [Base UI](https://base-ui.com/react/overview/quick-start) 提供弹窗、选择器、开关、复选框和按钮。支持桌面与手机布局，跟随 AstrBot 的亮色／暗色主题；目录筛选、分页和表情详情使用 Hash 路由，支持刷新与前进、后退。安装包已包含构建产物，部署时无需 Node.js 或前端构建步骤。

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
| `search_longhub_memes` | `query: string`, `limit: number = 5` | 难以提取标签或标签搜索无合适结果时，按文字、描述或含义回退检索；返回字符串 ID、文字、分级、描述及标签 |
| `query_longhub_memes_sql` | `sql: string`, `limit: number = 10` | 优先同时按英文、中文多个标签与同义词查询，取并集扩大匹配范围；也支持组合条件、统计和回退文字检索；最多返回 50 行 |
| `send_longhub_meme` | `image_id: string`, `position: string = "before"` | 模型选择 `before`（文字前）或 `after`（文字后）发送；单个入站事件最多发送一张 |

聊天模型需要支持工具调用，并在 AstrBot 中启用上述插件工具。候选只提供参考，没有合适的表情时模型可以不发送。所有分级默认开放，可在设置中限制；本地禁用和分级限制同时应用于普通搜索、SQL 查询与发送。

模型可以把**只发一张龙图、完全不附加文字**作为本轮的完整回复，不需要补确认语、解释或占位文字。是否配文由模型根据语境决定。为兼容拒绝空文本的模型，纯图回复使用插件会隐藏的内部结束标记，普通与流式输出均不会把标记发送到聊天里。

需要配文时，模型自主填写 `position`：`before` 会在文字前发送龙图；`after` 会先排队，等本轮正文的全部分段或流式输出完成后再发送。两者都支持空正文，只发图时无需为了触发发送而补话。旧调用省略该参数时仍默认为 `before`。排队的图片也计入每轮一张的限制；回复被取消或模型生成失败时不补发排队图片。

模型检索时优先从语境提取主体、作品、情绪、动作或场景等多个相关标签，同一轮同时使用英文、中文和常用同义词，兼顾 LONG Hub 标签与视觉模型标注。英文使用小写、下划线代替单词间空格，同时保留中文对应词，例如作品用 `genshin_impact`、`原神`，情绪用 `happy`、`开心`、`高兴`，贴脸素材可用 `龙脸`。

优先通过 SQL 查询 `meme_tags.tag`，先以 `IN/OR` 取相关标签的并集扩大匹配范围，再按命中标签数和对话语境挑选。只有语境明确要求多个概念同时成立时才在概念组之间使用 `AND`，组内仍取中英文与同义词的并集；避免要求同一图片同时拥有同一概念的中英文标签。仅在难以提取合适标签，或放宽标签条件、扩展同义词后仍没有合适结果时，再回落到图片文字、描述或自然语言意图检索。文字检索可保留中文等原语言。

普通检索使用 CJK 双字切分和英文词切分，配合 SQLite FTS5/BM25，匹配文字、描述和标签；模型可以用同义词重新搜索。近期使用的表情降低优先级，不依赖额外向量服务。

SQL 工具提供两张表，结构会直接写入工具说明，模型可自行组织查询：

| 表 | 字段 |
| --- | --- |
| `memes` | `id TEXT`、`text TEXT`、`rating TEXT`、`description TEXT`、`tags TEXT`（有效标签 JSON 数组）、`annotation_fresh INTEGER`（0/1） |
| `meme_tags` | `image_id TEXT`、`tag TEXT`，每张图的每个有效标签一行 |

`id` 使用十进制字符串，可直接交给发送工具；`description` 优先使用人工描述。有效标签已合并 Hub、AI 和人工来源并移除屏蔽标签；`annotation_fresh=1` 表示 AI 标注仍匹配当前图片、文字和提示词版本。标签按精确文本匹配；需要模糊匹配时可显式使用 `LIKE`，注意下划线在 `LIKE` 中是通配符，精确查询 `genshin_impact` 应使用 `=` 或 `IN`。例如，同时召回带有英文作品标签或中文情绪标注的图片，并优先展示命中多个标签的结果：

```sql
SELECT m.id, m.text, m.tags, COUNT(DISTINCT t.tag) AS matched_tags
FROM memes AS m
JOIN meme_tags AS t ON t.image_id = m.id
WHERE t.tag IN ('genshin_impact', '原神', 'happy', '开心', '高兴')
GROUP BY m.id
ORDER BY matched_tags DESC, CAST(m.id AS INTEGER) DESC
LIMIT 10;
```

支持单条 `SELECT` 或 `WITH … SELECT`，以及关联、子查询、聚合和 `json_each`。返回 `columns`、`rows`（按列排列的行数组）和 `truncated`；超出返回行数或大小时 `truncated=true`，可缩小条件或显式分页继续查找。SQL 最长 8192 字节，返回最多 50 行、32 KiB，并受执行时间和指令数限制。

每次查询从已提交目录生成独立的内存快照，只包含当前启用且符合分级设置的元数据。源数据库以只读方式打开，在执行模型 SQL 前已断开；SQL 无法访问 webhook 日志、设置、原始上游载荷、临时同步代次或外部文件。查询在后台线程执行，不占用目录写连接；同步、标注、人工修订与分级变化会在下次查询生效。发送时仍会重新校验条目是否可用。

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

视觉标注遵循本目录的素材命名约定：将用户所指的龙玉涛脸部轮廓抠图、P 到其他人物、角色或物体上的贴脸素材统一称为「龙脸」。模型会在描述中使用该名称，并加入 `龙脸` 标签，同时保留情绪、动作、场景等中文标签。这是素材名称约定；普通人脸、龙的脸或无法判断的模糊贴图不自动归入此类。

v0.1.5 更新了标注提示词版本，旧 AI 描述和标签仍可检索，但会显示为过期。可在管理页为旧标注重新排队以使用新规则；已启用的候选自动标注也会按原有限速逐项更新。人工标签、描述和屏蔽列表继续独立保留。

## 开发、检查与打包

Python 依赖由 `pyproject.toml` 与 `uv.lock` 管理。`requirements.txt` 为 AstrBot 插件安装器保留运行依赖入口。

前端源码位于 `frontend/`，依赖使用 `package.json` 与 `package-lock.json` 锁定，构建环境需要 Node.js 22.22 或以上。Vite 将静态产物输出到 `views/catalog/`，该目录随 Git 和插件包一同发布。修改前端后先构建，再执行浏览器验收与打包：

```powershell
cd frontend
npm ci
npm run format:check
npm run build
cd ..
```

`npm run dev` 提供本地 Vite 开发服务器；脱离 AstrBot 打开时会显示桥接连接提示。使用下方浏览器检查命令，可在本机通过内存中的桥接数据完整验收构建后的面板，不需要登录真实 AstrBot。

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

测试使用本机临时 HTTP 服务和可控的模型／AstrBot 接口替身，不需要真实 Appkey。覆盖流校验、切换竞态、延迟通知、持久化恢复、标签保护、模型任务和聊天工具边界。浏览器检查使用本机 Microsoft Edge，验证路由刷新、前进后退、筛选分页、详情编辑、超长 ID、错误重试、队列操作、密钥与表单保存、暗色主题和五个页面的手机布局，并在与 AstrBot 相同限制的沙箱 iframe 中验证桥接调用。图片和接口响应均为内存测试数据，截图保存在忽略的 `.preview-data/` 中。部署时仍需用真实 Appkey、公网 webhook、视觉提供商和 NapCat 完成联调。

协议来源：[LONG Hub Catalog Sync](https://docs.longhub.top/catalog-sync.html)、[Webhook](https://docs.longhub.top/webhook.html)、[AstrBot Plugin Views](https://docs.astrbot.app/en/dev/star/guides/plugin-pages.html)。参考 [smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) 和 [meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager) 的集成与管理交互，本插件独立实现。
