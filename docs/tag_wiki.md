# Tag Wiki：本地 E621 标签百科与智能检索

Tag Wiki 是一个完全本地化的 e621 标签百科镜像 + 检索系统，解决三个问题：

1. **查含义** —— 这个 tag 是什么意思？（中文摘要 + 英文原文 + 隐含搭配）
2. **语义搜索** —— 我想表达某个动作/画面，应该用什么 tag？（中文自然语言 → 向量检索英文 wiki → 推荐 tag）
3. **AI 问答** —— 把检索到的 wiki 内容交给已配置的在线大模型，生成带出处的中文回答（RAG）。

除「AI 问答」和「中文摘要翻译」需要联网调用你配置的 Provider 外，其余功能全部离线可用。

## 数据来源

- Wiki 正文：e621 官方 [db_export](https://e621.net/db_export/) 每日生成的 `wiki_pages-YYYY-MM-DD.csv.gz`（全量约 16 MB，站点仅保留最近数日）。构建时自动获取最新文件并缓存到 `data/tag_wiki/downloads/`。
- Tag 元数据（类别 / post_count / 别名 / **implications**）：复用数据集工作流的 `classify-snapshot-v1` 资源（`scripts/import_classification_snapshot.py` 导入）。未导入快照时查询接口返回 409 `wiki_tag_db_unavailable`。
- 中文译名：复用标签管理器的离线词典 `resources/tag_translations/` + 用户词典。
- 嵌入模型：`intfloat/multilingual-e5-small`（ONNX，约 470 MB），构建时经 Hugging Face 自动下载到 `data/tag_wiki/models/`。e5 协议（`query:` / `passage:` 前缀）+ mean pooling + L2 归一，中文提问可跨语言命中英文 wiki。

## 构建流程

在「Tag Wiki」页面的构建面板点击「下载/更新 Wiki 数据」，后台任务依次执行（进度轮询 `GET /status`）：

1. **download** —— 获取 db_export 列表 → 下载最新 `wiki_pages` dump（兼容带日期与无日期两种命名；同日已缓存则跳过；在线失败时回退本地缓存）。
2. **parse** —— 解析 DText：剥离标记语法、按标题切分章节（超过 1200 字符按段落续切）、提取 `[[wiki 链接]]`/`{{tag}}` 生成关联表；按 `updated_at` + 正文哈希增量入库。退化章节直接丢弃：短于 16 字符或少于 3 个词的碎片，以及"链接汤"章节（裸 URL、`thumb #编号` 占位符、纯站点链接列表——剥掉链接后不构成正文的），它们会以远高于正文的相似度霸占每一次语义检索。
3. **剪枝** —— 按标签类别剔除链接列表型页面（artist / character / contributor / invalid）的章节；再做一次"链接汤"形态清理，兜底覆盖标签库查不到类别的存根页面。页面本体保留供精确查询与 lookup。检索时还有一道查询时类别过滤兜底（超量抓取后剔除被排除类别的命中），即使索引与类别漂移也不会把链接列表页推荐给用户。
4. **model** —— 确保嵌入模型就位（缺失才下载）。
5. **embed** —— 对所有未嵌入章节批量生成向量（float32 BLOB 存入 SQLite；可重复构建，只补缺失部分；`force_reembed` 可全量重算）。

关键词检索依赖 SQLite FTS5（外部内容表 + 触发器同步）；若运行时 SQLite 意外缺少 FTS5 则自动回退 LIKE 子串匹配（`GET /status` 的 `index.fts_enabled` 可确认），中文查询主要由向量检索承担。

## 命令行（无需打开浏览器）

```bat
runtime\python.exe scripts\build_tag_wiki.py --status
runtime\python.exe scripts\build_tag_wiki.py --build
runtime\python.exe scripts\build_tag_wiki.py --translate --scope popular --min-post-count 1000 --max-pages 2000
runtime\python.exe scripts\build_tag_wiki.py --translate --scope model_vocab --provider <id>
```

`--build` 与 UI 按钮完全同管线；`--translate` 可反复执行直到覆盖目标范围（已翻译页面自动跳过）。

**本地 LLM 翻译**（不需要任何在线 Provider，用本机 GPU 跑 Qwen3-4B-Instruct）：

```bat
runtime\python.exe scripts\translate_tag_wiki_local.py --limit 2000 --batch-size 16
```

它复用与在线任务完全相同的提示词、JSON 解析与入库逻辑（摘要记录 `provider_id=local-qwen3-4b`），按 post_count 降序覆盖高频 tag；可反复执行直至覆盖整个范围。在线与本地两条路径的译文可互相覆盖更新。

## Danbooru wiki 语料（API 抓取，CLI 阶段）

Danbooru 没有类似 e621 `db_export` 的打包导出，wiki 语料（全量约 23 万页）通过官方 JSON API 分页抓取（`GET /wiki_pages.json`，每页最多 1000 条）。抓取器刻意保守：请求间隔默认 2 秒、`429` 遵循 `Retry-After`、瞬时失败指数退避、`4xx` 直接失败；翻页不依赖结果排序，只用 `page=b<游标>`（id 下边界）并取每批最小 id 前进；每个批次实时追加进 JSONL 原始缓存并落盘断点，中断后重跑即从断点继续，导入永远幂等（未变化页面跳过、上游已删除页面从库中清除）。

```bat
runtime\python.exe scripts\fetch_danbooru_wiki.py                    :: 首次全量遍历，之后自动增量
runtime\python.exe scripts\fetch_danbooru_wiki.py --max-requests 40  :: 每次只抓 40 个请求的预算，分多次跑
runtime\python.exe scripts\fetch_danbooru_wiki.py --skip-import      :: 只抓取不导入（--skip-fetch 反之）
runtime\python.exe scripts\fetch_danbooru_wiki.py --status
```

- **存储**：原始缓存与断点在 `data/tag_wiki/danbooru/`（`wiki_pages.jsonl` + `state.json`），页面与章节入库到独立数据库 `data/tag_wiki/tag_wiki_danbooru.sqlite3`（与 e621 库同 schema，互不影响）。
- **增量**：全量遍历完成后记录水位（UTC 日期），后续运行只抓 `updated_at` 落在水位之后的页面；某窗口填满一页时自动按时间对半拆分，不会静默截断。需要强制重抓可传 `--since YYYY-MM-DD`。
- **UI 与检索**：自 V1.10.1 起 Tag Wiki 页面右上角可切换 e621 / Danbooru 语料库，查含义、语义搜索与 AI 问答均按 profile 走各自的库；标签管理器的「查 Wiki」抽屉跟随当前会话的语料库。`build_tag_wiki.py --profile danbooru --build` 只刷新剪枝与向量索引（无需重新抓取）。
- **随包发行**：两个构建完成的数据库自 V1.10.1 起随发行包分发（`VACUUM INTO` 快照），解压即用，无需重建语料。

## 中文摘要（预翻译常用 tag）

构建完成后点击「翻译中文摘要」。每个页面**一次**模型调用，生成结构化 JSON（含义 / 用法 / 搭配建议 / 注意事项 + 相关 tag 列表），存入 `summaries` 表：

- 范围：`model_vocab`（默认，本地打标模型词表内的 tag，由 Runtime 注入） / `popular`（post_count ≥ 阈值） / `all`。
- 断点续跑：已翻译页面自动跳过，单次受 `max_pages` 限制，可反复启动直至覆盖全范围。
- Provider：请求可显式指定 `provider_id`/`model`，否则用第一个启用且已配置密钥的 Provider（与标签管理器翻译一致）；无可用 Provider 返回 409 `wiki_ask_unavailable`。

## API 一览（`/api/v1/tag-wiki`，同全局 authorize 依赖）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 数据库/索引/构建/翻译四组状态 |
| POST | `/build` (202) | 启动构建 `{download_dump, reindex, force_reembed}` |
| POST | `/translate` (202) | 启动翻译 `{scope, min_post_count, max_pages, provider_id?, model?}` |
| GET | `/translate/progress` | 翻译进度 |
| GET | `/lookup?tag=&profile=` | tag → 别名归一 + TagRef + implications + wiki 页（含中文摘要） |
| POST | `/search` | `{query, top_k}` → 章节命中（向量+关键词 RRF 融合）+ `suggested_tags` |
| POST | `/ask` | `{query, top_k, provider_id?, model?}` → RAG 中文回答 + 推荐 tags + 来源 |
| GET | `/page/{title}` | 单个 wiki 页全文（章节化） |

错误使用全局形状 `{code, message, fields, request_id, retryable}`。稳定 code：`wiki_not_built`、`wiki_busy`、`wiki_embed_model_unavailable`、`wiki_search_unavailable`、`wiki_ask_unavailable`、`wiki_tag_db_unavailable`、`wiki_page_not_found`（完整清单见 `backend/tagger2/tag_wiki/contracts.py`）。

## 前端

- 侧边栏「创作与处理」组新增 **Tag Wiki** 页：构建管理面板（状态徽标 + 构建/翻译按钮 + 进度条）+ 三个查询模式 Tab。
- TagManager 的标签编辑/展示栏与工作台 TagCloud 的 tag 药丸上有 **BookOpen 图标按钮**，点开 `WikiDrawer` 快查（含义摘要 + 隐含搭配 + 相关 tag）。
- 客户端 `frontend/src/lib/tagWiki.ts` 的类型与 `contracts.py` 的 TypedDict 一一对应。

## 目录与存储

```
data/tag_wiki/
├── tag_wiki.sqlite3            # e621：pages / chunks(+向量) / page_links / summaries / FTS5
├── tag_wiki_danbooru.sqlite3   # danbooru 镜像（同 schema，独立库）
├── danbooru/                   # danbooru API 抓取的原始 JSONL 缓存 + state.json 断点
├── downloads/                  # wiki_pages-*.csv.gz 缓存（保留最新）
└── models/                     # intfloat__multilingual-e5-small 快照
```

模块布局遵循 tag-manager 模板：`contracts.py`（pydantic 请求模型 + 响应 TypedDict）、`wiki_store.py`（SQLite，WAL + RLock + schema_migrations）、`importer.py`（e621 下载 + DText 解析 + 增量导入）、`danbooru_importer.py`（danbooru JSON API 分页抓取 + 增量导入）、`embedder.py`（ONNX/torch 双后端）、`searcher.py`（RRF 融合检索）、`translator.py`（摘要批任务）、`service.py`（编排 + 后台任务）、`api.py`（路由）。接线位于 `main.py` 的 `Runtime.__init__`（共享 tag 数据库与 provider 工厂，注入 `_tag_wiki_vocab`）与 `create_app`（SPA catch-all 之前挂载路由）。

## 运行参数

`config/app.toml`（参考 `app.example.toml`）支持可选的 `[tag_wiki]` 段：

```toml
[tag_wiki]
# 跨语言嵌入模型的 Hugging Face repo（首次构建自动下载，约 470 MB）；
# 无法直连 Hugging Face 时可改为镜像 repo 或本地 repo id。
embed_model_repo = "intfloat/multilingual-e5-small"
# 「高频标签」翻译范围 post_count 阈值的默认值（经 /status 下发，作为 UI 初始值）。
min_post_count = 1000
```

`embed_model_repo` 通过 `TagWikiService(embed_repo=...)` 注入；`min_post_count` 经 `GET /status` 的 `index.min_post_count` 下发给前端作为初始值。其余为代码内默认值（`contracts.py` / 各模块常量）：章节上限 `MAX_CHUNK_CHARS=1200`、ask 上下文预算 6000 字符/12 章节、摘要字段上限 400 字符。

## 测试

```bash
python -m pytest backend/tests/test_tag_wiki_store.py backend/tests/test_tag_wiki_importer.py backend/tests/test_tag_wiki_danbooru.py backend/tests/test_tag_wiki_embedder.py backend/tests/test_tag_wiki_searcher.py backend/tests/test_tag_wiki_service.py -q
npm --prefix frontend test -- --run
```

测试全程离线：下载/网络代码通过 httpx 假对象覆盖，嵌入模型用 4 维假向量替身。
