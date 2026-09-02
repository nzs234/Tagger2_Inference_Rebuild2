# 标签管理模块（Tag Manager）

标签管理是一个类似 BooruDatasetTagManager 的数据集标签编辑工作台：浏览图片网格、逐图或批量编辑标签、基于 e621/danbooru 官方标签库自动补全，并把所有修改原子化写回标注（sidecar）文件。

## 功能概览

- **数据集会话**：选择一个已注册的根目录（root）+ 相对路径打开一个数据集目录，后台扫描图片与标注文件并建立索引。支持递归扫描、增量刷新（按 mtime 差异）、多会话并行。
- **网格浏览**：虚拟化缩略图网格，按文件名 / 修改时间 / 标签数排序；按标签组合（包含 all/any、排除）、标注格式、有无 sidecar 过滤。
- **逐图编辑**：点击图片进入编辑面板，按格式提供编辑界面（见下），保存时带乐观并发校验（mtime 不一致返回 409）。
- **批量操作**：多选图片或按当前过滤器圈定范围，执行 添加 / 删除 / 替换（支持正则）标签。
- **撤销 / 重做**：每个会话保留最近 20 步操作日志，可逐步撤销与重做。
- **标签统计**：数据集内标签频次排行（带分类），点击即加入过滤。
- **标签库自动补全**：基于 workflow 模块的 classify-snapshot 资源（官方 DB 导出），返回名称、分类、post_count 与别名指向。
- **中英双语显示**：所有标签（网格卡片、编辑面板、自动补全、统计榜）都可同时显示英文原名与中文译名，词库随仓库离线提供，可一键关闭。
- **下划线 / 空格切换**：切换标签分隔符风格。该开关同时决定保存时写入 sidecar 的拼写，与 BooruDatasetTagManager 的行为一致。
- **NL 在线翻译**：九字段的 `nl` 段落可调用已配置的在线大模型翻译（中↔英），结果需显式点击「替换 NL」才会写入草稿。

## 中英双语标签

标签库本身只有英文，因此中文译名来自随仓库提交的离线词库 `resources/tag_translations/`：

| 文件 | 条目数 | 说明 |
| --- | --- | --- |
| `danbooru-zh.csv.gz` | 310,617 | Danbooru 词库（含别名条目） |
| `e621-zh.csv.gz` | 68,399 | Danbooru 词条 ∩ e621 标签命名空间，再叠加 e621 专有词汇表 |
| `e621-supplement-zh.csv` | 297 | 仓库内维护的明文词汇表，可直接编辑补充 |

词库在首次使用时按 profile 加载一次并常驻进程，查找键为「小写 + 下划线」，所以 `Blue Eyes`、`blue_eyes`、`BLUE_EYES` 都能命中同一条译名。词库缺失或损坏不会影响编辑功能：标签退回纯英文显示，工具栏给出提示。

数据来源与许可见 [`resources/tag_translations/README.md`](../resources/tag_translations/README.md)（其中两个社区来源未声明许可，对外分发前请自行确认）。重新生成：

```bat
runtime\python.exe scripts\build_tag_translations.py
runtime\python.exe scripts\build_tag_translations.py --sources amenorira   :: 仅使用 MIT 来源
```

## 下划线 / 空格开关

工具栏的「下划线 / 空格」开关是显示与写入的统一设置：

- 显示：`blue_eyes` ↔ `blue eyes`，作用于网格、编辑面板、自动补全、统计榜与过滤器回显。
- 写入：保存单图时，`tag_txt` 的全部标签、`tags_json` 的每个 `text`、九字段的 `quality` / `appearance` / `tags` / `environment` 四个列表都会转成当前风格；`nl`、`character`、`series`、`artist`、`count` 不受影响。批量操作的标签与替换值同样转换，但**正则模式下的模式串与替换串保持原样**（模式不是标签名）。
- 过滤：后端按「小写 + 下划线」归一化比较，因此用哪种风格输入过滤条件都能匹配到磁盘上的另一种拼写。

## NL 在线翻译

九字段编辑器的 `nl` 段落下方提供翻译面板：选择方向（译为中文 / 译为英文）、在线模型与可选的模型 ID，点击「翻译」调用 `POST /tag-manager/nl/translate`。翻译结果只展示，需点击「替换 NL」才会写入草稿，避免覆盖用户正在编辑的文本。

模型下拉只列出**已启用且已配置密钥**的 provider；没有任何可用模型时接口返回 409 `nl_translate_unavailable`，界面提示先到「Provider 配置」页添加并启用一个在线模型。翻译本身不落盘，只有保存整张图片时才写入 sidecar。

## 支持的标注格式

| 格式 | 判定 | 读写 |
| --- | --- | --- |
| 平面 TXT（booru 逗号分隔标签） | `<图片名>.txt` 非空 | 可编辑 |
| 本地标签 JSON（`{"tags": [...]}`，对象条目可带 category/score） | `.json` 且仅含 tags 容器 | 可编辑 |
| 九字段 Anima JSON（workflow 标准 JSON） | `.json` 含九字段中除 tags 外的任一键 | 可编辑（九字段表单） |
| raw e621 分组 JSON | 9 个分组键齐全 | 只读（fail-closed，与 workflow 导入器一致） |
| 无标注 | — | 保存时按所选格式创建 sidecar |

约定：JSON 序列化与 `artifacts.atomic_write_json` 一致（`ensure_ascii=False, indent=2` + 换行）；TXT 为 `", "` 连接 + 末尾换行；点分文件名（如 `43900,_(artist).png`）的 sidecar 配对保持点号。批量标签操作作用于九字段的 `tags` / `appearance` / `environment` 三个列表字段，永不触碰 `nl` / `character` / `series` / `artist` / `quality` / `count`。

## 快速上手

1. **（可选）导入标签库**：e621 快照（`classify-e621-*-v1`）与 danbooru 快照（`classify-danbooru-*-v1`）都随发布包提供。若要自行重建 danbooru 快照，先取得标签表与别名表 CSV（danbooru.donmai.us 的 DB 导出；该站对无浏览器的直接下载有 Cloudflare 拦截，也可用 `a1111-sd-webui-tagcomplete` 的 MIT 授权 `tags/danbooru.csv` 转换出同形状的两张表），再用脚本导入注册：
   ```bat
   runtime\python.exe scripts\import_classification_snapshot.py ^
     --profile danbooru ^
     --tags-csv D:\snapshots\danbooru\tags.csv ^
     --aliases-csv D:\snapshots\danbooru\tag_aliases.csv ^
     --resource-id classify-danbooru-20260901-v1 ^
     --allow-official-anomalies ^
     --anomaly-report data\workflows\resources\classify\classify-danbooru-20260901-v1.anomalies.json
   ```
   `GET /api/v1/tag-manager/tag-db/info` 可查看各 profile 的快照与中文词库状态。
2. **打开数据集**：在页面左侧选择输入根目录、输入相对路径、选择 profile（e621 / danbooru），点击打开。扫描期间会话状态为 `indexing`，完成后自动变为 `ready`。
3. **浏览与编辑**：网格中点选图片 → 右侧编辑面板按格式编辑 → 保存。多选后使用批量操作条。
4. **撤销**：工具栏撤销/重做按钮按操作日志逐步回退。

## API 概览

路由前缀 `/api/v1/tag-manager`（与其它模块一致，挂载在共享的 authorize 依赖之后）：

```
POST   /datasets                       建会话并后台索引（202 + 轮询）
GET    /datasets                       会话列表
GET    /datasets/{id}                  会话详情（状态/计数）
DELETE /datasets/{id}                  删除会话（索引与日志）
POST   /datasets/{id}/refresh          mtime 增量重扫（202）
GET    /datasets/{id}/images           分页/过滤/排序的图片列表（含标签）
GET    /datasets/{id}/images/{iid}     图片详情 + 格式原生内容 + sidecar_mtime
PATCH  /datasets/{id}/images/{iid}     保存编辑（content 按 kind 判别；expected_sidecar_mtime 乐观锁）
POST   /datasets/{id}/batch            批量 add/remove/replace（image_ids 或 filter 二选一）
POST   /datasets/{id}/undo             撤销最近一步
POST   /datasets/{id}/redo             重做
GET    /datasets/{id}/tags/stats       标签频次统计（含 translation）
GET    /datasets/{id}/images/{iid}/thumbnail?size=256   缩略图（JPEG，磁盘缓存）
GET    /tag-db?profile=&query=&limit=  标签库自动补全（含 translation）
GET    /tag-db/info                    标签库快照 + 中文词库状态
POST   /translations/lookup            批量查询中文译名（最多 500 个标签）
POST   /nl/translate                   用在线模型翻译 NL 段落（target=zh|en）
```

错误统一为 `{"detail": {"code", "message", "retryable"}}`（应用的错误中间件会把它平铺到响应体并附加 `request_id`）；常见错误码：`sidecar_conflict`（mtime 过期，重新加载即可）、`sidecar_kind_mismatch`（编辑负载与 sidecar 格式不符）、`sidecar_read_only`（raw e621 只读）、`batch_too_large`（单批上限 2000 张）、`root_not_writable`（数据集根目录未开启可写）、`tag_db_unavailable`（该 profile 没有已注册的分类快照）、`nl_translate_unavailable`（没有已启用且已配置密钥的在线模型）、`nl_translate_failed`（在线模型调用失败，可重试）。

## 架构与数据存储

```
backend/tagger2/tag_manager/
├─ api.py         路由（/api/v1/tag-manager）
├─ service.py     会话、索引、编辑、批量、撤销重做、双语标注、NL 翻译编排
├─ sidecar_io.py  三种可编辑格式 + raw e621 的读判/渲染/原子写
├─ storage.py     SQLite 索引（sessions / images / image_tags / undo_journal）
├─ tag_db.py      e621/danbooru 标签库进程级索引（复用 workflow 资源）
├─ translations.py 离线中文词库加载与查询（进程级缓存，缺失即降级）
├─ thumbnails.py  缩略图生成与磁盘缓存
└─ contracts.py   严格请求模型（pydantic，extra="forbid"）
```

- 独立数据库 `data/tag_manager/tag_manager.sqlite3`；缩略图缓存 `data/tag_manager/thumbnails/`。与 jobs / workflows / image_generation 三个库严格分离，删除会话只清索引与日志，不触碰数据集文件。
- 图片 id 跨刷新稳定（按相对路径 upsert），编辑选中项不会因重扫失效。
- 索引仅保存标签的规范化视图；编辑面板始终从磁盘实时读取 sidecar 内容，保存前以 mtime 校验外部修改（fail-closed）。

## 安全模型

- 路径访问只接受 `root_id + relative_path`，经共享 `PathAllowlist` 解析，响应不返回绝对路径。
- 所有 sidecar 写入为原子写（临时文件 + fsync + replace）。
- sidecar 读取上限 1 MiB、缩略图解码前执行字节/像素预算校验，防解压炸弹。
- 会话 id 与图片 id 均不可枚举或跨会话访问。

## 与 Dataset Workflow 的关系

标签管理面向“人工修标注”的交互场景；Dataset Workflow 面向“事务化批量流水线”。两者共享：九字段契约、classify-snapshot 标签库资源、sidecar 格式判定规则、原子写与路径安全原语。在 workflow 中判定为 `tag_txt` / `standard_json` 的数据集可直接在标签管理中打开并继续加工；raw e621 JSON 保持只读以维持上游字节级兼容承诺。
