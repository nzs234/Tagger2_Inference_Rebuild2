# Tag Wiki：本地标签目录（Catalog）+ e621/Danbooru 标签百科

Tag Wiki 面向用户的主界面是一个**本地高频 TAG 目录**（类似官方 booru Wiki 的浏览体验）：按官方分类 / 语义分组浏览、按名称与别名模糊搜索、点开查看 Wiki 词条、中文摘要与关联标签。目录只收录 `post_count >= 100` 的高频标签，由维护端 CLI 生成，用户端只读。

保留的非首页功能：

1. **查含义**（`/lookup`）—— TagManager「查 Wiki」抽屉等既有消费者继续使用。
2. **中文摘要翻译**（`/translate`）—— 维护者/CLI 专属，为高频页面预生成结构化中文摘要。

> 历史版本（≤ V1.10.4）提供的语义向量检索（`/search`）与 AI 问答（`/ask`）已随嵌入模型栈一起移除：目录搜索按标签名称/别名排序，不再依赖任何模型。除「中文摘要翻译」需要联网调用配置的 Provider 外，其余功能全部离线可用。

## 数据来源

- Wiki 正文：e621 官方 [db_export](https://e621.net/db_export/) 每日生成的 `wiki_pages-YYYY-MM-DD.csv.gz`（全量约 16 MB，站点仅保留最近数日）。构建时自动获取最新文件并缓存到 `data/tag_wiki/downloads/`。
- Tag 元数据（类别 / post_count / 别名 / **implications**）：复用数据集工作流的 `classify-snapshot-v1` 资源（`scripts/import_classification_snapshot.py` 导入）。未导入快照时查询接口返回 409 `wiki_tag_db_unavailable`。
- 中文译名：复用标签管理器的离线词典 `resources/tag_translations/` + 用户词典。

## 构建流程

维护者运行 `scripts/build_tag_wiki.py --build`（或维护端点 `POST /build`），后台任务依次执行（进度轮询 `GET /status`）：

1. **download** —— 获取 db_export 列表 → 下载最新 `wiki_pages` dump（兼容带日期与无日期两种命名；同日已缓存则跳过；在线失败时回退本地缓存）。
2. **parse** —— 解析 DText：剥离标记语法、按标题切分章节（超过 1200 字符按段落续切）、提取 `[[wiki 链接]]`/`{{tag}}` 生成关联表；按 `updated_at` + 正文哈希增量入库。退化章节直接丢弃：短于 16 字符或少于 3 个词的碎片，以及"链接汤"章节（裸 URL、`thumb #编号` 占位符、纯站点链接列表——剥掉链接后不构成正文的）。
3. **剪枝** —— 按标签类别剔除链接列表型页面（artist / character / contributor / invalid）的章节；再做一次"链接汤"形态清理，兜底覆盖标签库查不到类别的存根页面。页面本体保留供精确查询、lookup 与目录详情展示。

构建不再生成向量索引；章节（chunks）仅作为页面的结构化正文存储。构建完成后请重跑目录 CLI（见下）刷新浏览数据。

## 命令行（无需打开浏览器）

```bat
runtime\python.exe scripts\build_tag_wiki.py --status
runtime\python.exe scripts\build_tag_wiki.py --build
runtime\python.exe scripts\build_tag_wiki.py --translate --scope popular --min-post-count 1000 --max-pages 2000
runtime\python.exe scripts\build_tag_wiki.py --translate --scope model_vocab --provider <id>
runtime\python.exe scripts\build_tag_wiki.py --translate --profile danbooru --scope popular --min-post-count 1000 --provider cpa --concurrency 8
```

`--build` 与维护端点同管线；`--translate` 可反复执行直到覆盖目标范围（已翻译页面自动跳过）。`--concurrency` 控制并行翻译页数（默认 4，上限 12；上游限流时调回 1）。

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
- **UI 与语料库**：Tag Wiki 页面右上角可切换 e621 / Danbooru 语料库，目录浏览与查含义均按 profile 走各自的库；标签管理器的「查 Wiki」抽屉跟随当前会话的语料库。`build_tag_wiki.py --profile danbooru --build` 只刷新剪枝（无需重新抓取）。
- **随包发行**：两个构建完成的数据库自 V1.10.1 起随发行包分发（`VACUUM INTO` 快照），解压即用，无需重建语料。

## 中文摘要（预翻译常用 tag）

构建完成后运行 `--translate`。每个页面**一次**模型调用，生成结构化 JSON（含义 / 用法 / 搭配建议 / 注意事项 + 相关 tag 列表），存入 `summaries` 表：

- **范围**：`--scope` 支持 `model_vocab`（本地打标模型词表内的页面，默认）、`popular`（`--min-post-count` 阈值以上）、`all`。
- **幂等**：已有摘要的页面自动跳过，`--max-pages` 限制单次页数。
- **JSON 语义校验**：模型回复经解析、逐字段截断后入库；相关 tag 列表在 lookup 时与标签库对照（未知 tag 不展示）。

## 标签目录（Catalog，schema v2 起）

Tag Wiki 首页的数据来自 catalog 表（`catalog_tags` / `catalog_relations` / `catalog_meta`，wiki 数据库 schema v1 → v2 增量迁移，老库打开即自动升级，页面/chunk/摘要不受影响）。**用户端与发行端严格只读**——目录唯一的写入口是维护端 CLI：

```bat
runtime\python.exe scripts\build_tag_wiki_catalog.py                     :: 全部 profile（e621 + danbooru）
runtime\python.exe scripts\build_tag_wiki_catalog.py --profile e621 --min-post-count 200
runtime\python.exe scripts\build_tag_wiki_catalog.py --dry-run           :: 只打印统计不写库
runtime\python.exe scripts\build_tag_wiki_catalog.py --status            :: 查看各 profile 目录元数据
```

构建过程（无模型调用、默认不联网）：

1. 加载运行时分类快照（`classify-snapshot-v1`，与打标/分类共用），取全部 `post_count >= --min-post-count`（默认 100）的 canonical tag；别名不入目录，查询时经 TagDatabase 归一。
2. 用确定性两级分类（`backend/tagger2/tag_wiki/taxonomy.py`）打分组：一级为官方 category；`general` 类按可维护的英语 token 规则 + overrides 划分语义组（`appearance_body` 外观体型 / `action_pose` 动作姿势 / `body_part` 身体部位 / `clothing` 服装 / `sexual` 性内容 / `other_general` 兜底；species/character/copyright/meta/artist 等官方分类直接映射到同名稳定组）。改规则后请递增 `TAXONOMY_VERSION`。
3. 组合关系（**不做在线共现抓取**）：tag-database implications（正向入库、查询时反查反向）+ 本地 wiki `page_links`；关系两端都必须在目录内，UI 上的关联标签必然可点且高频。
4. 单事务整体重建（`WikiStore.replace_catalog`）并写入 meta（阈值、taxonomy version、生成时间、数量统计）；可重复执行，wiki 正文/摘要永不被触碰。

**维护顺序**：每次重建 wiki 语料（`build_tag_wiki.py`）或分类快照后，发行前必须重跑本 CLI 生成目录。`scripts/build_release.ps1` 会在打包阶段校验两个 staged 库的目录（表存在、非空、无 `post_count < 100` 的标签、meta 阈值 ≥ 100、无目录外关系端点），并在冒烟测试中对两个 profile 调用 `/catalog/categories`——目录缺失或过低频会直接终止打包。

目录 API（同 `/api/v1/tag-wiki` 前缀，只读，frozen 模式可用）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/catalog/categories` | 分类树（各组数量、目录 meta） |
| GET | `/catalog/tags` | 浏览/搜索：`category` `group` `q` `offset` `limit`；`q` 支持 精确 canonical > 精确别名 > 前缀 > 词前缀 > 包含 排序（同级按 post_count 降序、名称升序），每项带 `match` 标注 |
| GET | `/catalog/tags/{title}` | 目录词条详情：tag 信息 + 原 wiki 页（章节/中文摘要）+ 关系（`implications` / `wiki_links` / `cooccurrences` 分组，含方向；每组按目标热度排序后截断至 30 条，防止枢纽标签的数百条关联淹没详情） |

错误码：`wiki_catalog_missing`（409，目录未生成——先跑上面的 CLI）、`wiki_catalog_tag_not_found`（404，目录中无此标签，通常低于收录阈值）。目录未生成时旧 `/lookup`、`/page` 完全不受影响；`/lookup` 的 `related_tags` 行为保持原样（不按频次过滤）。

## API 一览（`/api/v1/tag-wiki`，同全局 authorize 依赖）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 数据库/目录/构建/翻译状态 |
| POST | `/build` (202) | 启动构建 `{download_dump, reindex}` |
| POST | `/translate` (202) | 启动翻译 `{scope, min_post_count, max_pages, concurrency, provider_id?, model?}` |
| GET | `/translate/progress` | 翻译进度 |
| GET | `/lookup?tag=&profile=` | tag → 别名归一 + TagRef + implications + wiki 页（含中文摘要） |
| GET | `/page/{title}` | 单个 wiki 页全文（章节化） |
| GET | `/catalog/categories` | 目录分类树 |
| GET | `/catalog/tags` | 目录浏览/搜索 |
| GET | `/catalog/tags/{title}` | 目录词条详情 |

错误使用全局形状 `{code, message, fields, request_id, retryable}`。稳定 code：`wiki_not_built`、`wiki_busy`、`wiki_ask_unavailable`（翻译未配置 Provider）、`wiki_tag_db_unavailable`、`wiki_page_not_found`、`wiki_catalog_missing`、`wiki_catalog_tag_not_found`（完整清单见 `backend/tagger2/tag_wiki/contracts.py`）。

## 前端

- 侧边栏「创作与处理」组 **Tag Wiki** 页：只读状态面板（Wiki 页数/章节数/已翻译摘要/Dump 日期/标签目录徽标）+ 高频标签目录（左侧分类侧栏、二级语义组筛选、60 个/页可翻页的标签列表、防抖搜索框支持 ↑↓/Enter/Escape 键盘导航、同页词条详情含中文摘要/Wiki 章节摘要/隐含与关联标签，返回列表后筛选保留）。构建与中文翻译是维护者/CLI 专属操作（`scripts/build_tag_wiki.py`、`scripts/build_tag_wiki_catalog.py`），前端不提供维护入口；成品包 frozen 模式下后端同样拒绝（403）。
- TagManager 的标签编辑/展示栏与工作台 TagCloud 的 tag 药丸上有 **BookOpen 图标按钮**，点开 `WikiDrawer` 快查（含义摘要 + 隐含搭配 + 相关 tag）——抽屉继续走旧 `/lookup` API，与目录页互不影响。
- 客户端 `frontend/src/lib/tagWiki.ts` 的类型与 `contracts.py` 的 TypedDict 一一对应。

## 目录与存储

```
data/tag_wiki/
├── tag_wiki.sqlite3            # e621：pages / chunks / page_links / summaries / catalog_*(schema v3)
├── tag_wiki_danbooru.sqlite3   # danbooru 镜像（同 schema，独立库）
├── danbooru/                   # danbooru API 抓取的原始 JSONL 缓存 + state.json 断点
└── downloads/                  # wiki_pages-*.csv.gz 缓存（保留最新）
```

模块布局遵循 tag-manager 模板：`contracts.py`（pydantic 请求模型 + 响应 TypedDict）、`wiki_store.py`（SQLite，WAL + RLock + schema_migrations；v2 起 catalog 表，v3 起移除向量/FTS 结构）、`importer.py`（e621 下载 + DText 解析 + 增量导入）、`danbooru_importer.py`（danbooru JSON API 分页抓取 + 增量导入）、`translator.py`（摘要批任务）、`taxonomy.py`（目录两级分类，纯规则）、`service.py`（编排 + 后台任务 + 目录只读查询）、`api.py`（路由）。接线位于 `main.py` 的 `Runtime.__init__`（共享 tag 数据库与 provider 工厂，注入 `_tag_wiki_vocab`）与 `create_app`（SPA catch-all 之前挂载路由）。

## 运行参数

`config/app.toml`（参考 `app.example.toml`）支持可选的 `[tag_wiki]` 段：

```toml
[tag_wiki]
# 「高频标签」翻译范围 post_count 阈值的默认值（经 /status 下发，作为 UI 初始值）。
min_post_count = 1000
# 成品包模式：true 时 /build 与 /translate 直接返回 403（code: wiki_frozen），
# 前端构建面板隐藏全部维护入口。发布者本地用 false 构建数据，随包分发时置 true。
frozen = false
```

`min_post_count` 通过 `TagWikiService(default_min_post_count=...)` 注入。其余为代码内默认值（`contracts.py` / 各模块常量）：章节上限 `MAX_CHUNK_CHARS=1200`、摘要字段上限 400 字符、目录搜索候选上限 1000、关系分组展示截断 30 条。`GET /status` 另返回顶层 `frozen` 与每 profile 的 `catalog` 状态（是否已生成 / 标签数 / 阈值 / 生成时间）。

### 成品包分发（frozen 模式）

面向最终用户的包携带已构建完成的 wiki 数据库（含目录表）；发行脚本在打包阶段强制把包内 `config/app.toml` 置为 `frozen = true`（开发机本地保持 `frozen = false`）：构建与中文翻译只由发布者执行，后端 403、前端隐藏维护面板，用户开箱即用。发行打包不依赖任何模型文件；历史版本（≤ V1.10.4）曾单独分发 Qwen3 嵌入模型权重用于本地语义检索，该功能移除后新版本不再需要。

## 测试

```bash
python -m pytest backend/tests/test_tag_wiki_store.py backend/tests/test_tag_wiki_importer.py backend/tests/test_tag_wiki_danbooru.py backend/tests/test_tag_wiki_service.py backend/tests/test_tag_wiki_catalog.py -q
npm --prefix frontend test -- --run
```

测试全程离线：下载/网络代码通过 httpx 假对象覆盖；catalog 各层（store 迁移/读写、taxonomy 规则、构建 CLI、service 排序与关系分组、API 契约与 profile 隔离）由 `test_tag_wiki_catalog.py` 覆盖。
