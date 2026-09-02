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

1. **（可选）导入标签库**：e621 快照（`classify-e621-*-v1`）通常已在 workflow 资源目录注册；danbooru 需先下载官方 DB 导出（danbooru.donmai.us 的 tags / tag_aliases / tag_implications CSV），再用脚本导入注册：
   ```bat
   runtime\python.exe scripts\import_classification_snapshot.py ^
     --profile danbooru ^
     --tags-csv D:\snapshots\danbooru\tags.csv ^
     --aliases-csv D:\snapshots\danbooru\tag_aliases.csv ^
     --implications-csv D:\snapshots\danbooru\tag_implications.csv ^
     --resource-id classify-danbooru-20260901-v1
   ```
   `GET /api/v1/tag-manager/tag-db/info` 可查看各 profile 的可用与已加载状态。
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
GET    /datasets/{id}/tags/stats       标签频次统计
GET    /datasets/{id}/images/{iid}/thumbnail?size=256   缩略图（JPEG，磁盘缓存）
GET    /tag-db?profile=&query=&limit=  标签库自动补全
GET    /tag-db/info                    标签库可用/加载状态
```

错误统一为 `{"detail": {"code", "message", "retryable"}}`；常见错误码：`sidecar_conflict`（mtime 过期，重新加载即可）、`sidecar_kind_mismatch`（编辑负载与 sidecar 格式不符）、`sidecar_read_only`（raw e621 只读）、`batch_too_large`（单批上限 2000 张）。

## 架构与数据存储

```
backend/tagger2/tag_manager/
├─ api.py         路由（/api/v1/tag-manager）
├─ service.py     会话、索引、编辑、批量、撤销重做编排
├─ sidecar_io.py  三种可编辑格式 + raw e621 的读判/渲染/原子写
├─ storage.py     SQLite 索引（sessions / images / image_tags / undo_journal）
├─ tag_db.py      e621/danbooru 标签库进程级索引（复用 workflow 资源）
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
