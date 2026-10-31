# LONG Hub · MaiBot 龙图插件

此分支将 AstrBot LONG Hub 插件迁移到 MaiBot，版本 **0.6.1**。原 AstrBot 0.5.1 代码保留在 `main` 分支。当前分支的安装入口为 `plugin.py`，发行 ZIP 只包含 MaiBot 运行所需文件。

同步 LONG Hub 目录、补全视觉标注，通过语义、标签 SQL 或文字寻找表情。图片从 S3 兼容的 R2 存储按需读取，发送、GIF 抽帧和 WebUI 缩略图均在内存中处理，不创建本地图片库。

## 运行要求

- Python 3.12+。
- MaiBot **1.3.19** 的新版隔离插件运行时，SDK **2.20+（<3）**；本次对照 MaiBot `main` 提交 `c6429547af711555e3d4d1b8d04b413572cb616c`（2026-10-30），本地验证使用 SDK 2.21.0。
- WebUI 必须配套支持 `webui.json schema_version: 2` 的 dashboard（`custom_pages` 能力）。仅升级 SDK 不足以获得新版页面协议。
- LONG Hub 有效 Appkey、webhook 订阅及密钥。模型密钥由 MaiBot 管理。

原生表情注册接口需要落盘，因此本插件维护独立目录，并通过原生 ReplyExtension 和 `emoji` 消息段接入聊天。不会导入 MaiBot 的内置本地表情库；宿主原有的 `send_emoji` 功能仍由宿主管理。

## 安装与配置

1. 解压发行 ZIP，把 `maibot_plugin_longhub` 目录放入 MaiBot 的 `plugins/`。让宿主安装 manifest 声明的 Python 依赖；手工部署时也可使用 `uv pip install --python <Runner的Python> -r requirements.txt`。
2. 在 MaiBot 插件配置中设置 `[plugin] enabled = true`，重载插件。启用开关由宿主管理，其余业务配置通过 LONG Hub 管理页面保存到插件数据目录的 `settings.json`。
3. 打开 MaiBot WebUI 的 **LONG Hub** 页面，在「连接与设置」填写 Appkey、webhook 密钥和监听地址。默认接收地址为 `127.0.0.1:8791/hooks/longhub`。Docker 部署需把监听地址改为 `0.0.0.0` 并映射端口。
4. 用反向代理把公网 HTTPS webhook 地址转发到上述接收器，在 LONG Hub 完成订阅后开启「已在 LONG Hub 启用订阅」。也可保存密钥后，在概览点击「确认订阅并校准」：它会保留已有密钥、保存订阅确认并启动校准。未确认时会明确显示「待确认订阅」，缺少密钥或接收器未启动时会说明具体原因。后台仅在订阅确认后发起初次全量同步，避免同步期间遗漏通知。
5. 视觉标注选择原生支持图片的 MaiBot 模型任务（例如 `vlm`）；语义检索选择 `embedding`。请在宿主中先配置对应模型。

管理 API 只通过 MaiBot 已认证的页面桥接开放。8791 端口仅接收带签名的 webhook，不提供匿名管理页面。项目没有指定代理，默认使用部署环境的网络。

插件数据通过 `ctx.paths.data_dir` 保存，通常为 `data/plugins/movetoex.longhub/`；以宿主实际分配路径为准。请备份该目录中的数据库和配置文件。

## 聊天行为

- 决定参与对话后，提示模型每轮实际调用合适的检索工具，再判断候选是否贴切；旁听或等待时不要求搜索。
- 当上下文有意义且回应明确相关时，标注覆盖率 **严格 >50%**、当前索引完整后优先使用语义检索。否则按标签 SQL 检索，再回退到图片文字/描述。
- 标签查询同时使用英文下划线标签与中文同义词，通过 IN/OR 扩大匹配，例如 `genshin_impact`/`原神`。模型可以通过只读 SQL 精确查找，数据库写操作、文件访问与任意扩展均被禁止。
- 配文使用 `reply` 的 `plugin_options["movetoex.longhub.meme"]`：`image_id`、`position: "before" | "after"`、`only_image: boolean`。图在文本前后由模型选择。
- 纯图使用 `send_longhub_meme`；成功后结束本轮，不追加确认。图片文字已能完整表达意思时倾向只发图。
- `/l` 请求宿主根据当前上下文检索并发送一张图；不再拦截空 @bot。
- 成功发送由 MaiBot 原生回调核对会话、图片 hash 与唯一回执，再记录发送事实；正常图文由宿主写入聊天历史，纯图也要求同步原生历史。仅准备好图片或调用发送函数不算发送成功。
- 工具调用以 INFO 输出参数，语义失败记录错误链；候选元数据按不可信数据处理。

以上是模型指导，不保证模型每轮遵循。最终回复是否触发、群聊并发调度与其他插件对消息的改写，仍由 MaiBot 及其配置控制。

## 目录、标注和向量

初次使用 `/sync/full`。启动、定时同步和手工同步前检查 `/sync/version`，远端更改时间戳与本地一致时跳过完整导出；可手工强制同步。webhook 先持久化通知，再异步处理，支持日志查看与失败重试。

Hub 标签、视觉标签、人工标签和排除标签独立保留。视觉模型会把被 P 上去的龙玉涛脸称为「龙脸」。GIF 使用四帧拼图输入，并明确告知模型这是一段动图的时间采样，避免标成四格漫画。

MaiBot 目前没有公开的插件向量库 API。本分支使用宿主 `llm.embed` 生成向量，通过 SQLAlchemy 在插件目录持久化归一化 float32 向量，在工作线程分批计算余弦相似度，不依赖 AstrBot、FAISS 或 NumPy。检索复杂度为 O(图库向量数 × 维度)，大图库或高维模型需要评估延迟；不会把整个向量库一次性载入内存。模型/维度变化会拒绝混用旧向量，重新保存设置或宿主重载模型配置后重建。

面板保留 React + Vite + React Router + Tailwind + Base UI，使用 Chart.js 展示标注和 Embedding 进度。单文件 HTML 不依赖 CDN、Node 运行环境或外部静态资源；R2 缩略图由原生 API 返回内存中的 JPEG data URL，最多缓存 8 MiB。

MaiBot 的 IPC 单帧上限为 16 MiB。本插件限制原图为 11 MiB，并额外检查整组回复大小，超出时要求换图；`max_image_mb` 仍控制下载及标注上限。

## 从 AstrBot 导入

先备份、停止两端插件，准备一个**尚不存在**的 MaiBot 目标数据目录，然后在本插件目录运行：

```sh
uv run python -m tools.migrate_astrbot \
  --astrbot-data /path/to/astrbot/plugin_data/astrbot_plugin_longhub \
  --data-dir /path/to/maibot/data/plugins/movetoex.longhub \
  --config /path/to/astrbot/data/config/astrbot_plugin_longhub_config.json
```

`--config` 可省略。不省略时会保留 Hub 配置与密钥，但清空旧框架模型 ID，并关闭 webhook 订阅确认。工具只接受 schema v2（AstrBot 0.5.x）的数据库，使用 SQLite backup API 复制一致快照；拒绝覆盖现有目录，来源保持不变。

保留目录、手工修订、有效视觉标注、任务与 webhook 日志。不复制图片或旧 FAISS 索引。导入后在新面板重新选择模型、检查 webhook 地址、确认订阅；向量索引自动重建。避免两套实例同时处理同一订阅。原 AstrBot 的会话 ID 与 MaiBot 不兼容，旧发送记录不会用于新会话去重。

## 开发与验证

```sh
uv sync --locked --group dev --group browser
uv run pytest
uv run ruff check .
cd frontend
npm ci
npm run build
cd ..
uv run --group browser python -m tools.check_ui --maibot
uv run python -m tools.package
```

浏览器检查使用系统 Edge，仅运行内存中的桥接和图片测试数据。`views/maibot/index.html` 已预构建在发行包中。`npm run build -- --mode astrbot` 可另行构建历史面板以运行原 AstrBot 回归检查。`tools/check_maibot_contract.py` 可对照指定 MaiBot 源码校验原生声明和上下文协议。

目前完成本地 SDK/数据库/发送回执与浏览器沙箱验证，尚未部署到实际 MaiBot/QQ 实例进行端到端验收。

插件尚未确定公开仓库，manifest 的 `https://example.invalid/maibot_plugin_longhub` 是明确的占位地址，不是可用下载来源，发布前需要替换。

接口参考：[LONG Hub 文档](https://docs.longhub.top)、[MaiBot 插件文档](https://docs.maibot.chat/plugin/)、[ReplyExtension](https://docs.maibot.chat/plugin/reply-extensions)、[WebUI 页面协议](https://docs.maibot.chat/plugin/webui-pages)。
