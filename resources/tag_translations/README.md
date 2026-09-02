# 标签中文翻译词库（离线）

标签管理页面双语显示所用的英文 → 简体中文词库。文件已随仓库提交，**无需联网**即可使用。

## 文件

| 文件 | 说明 |
| --- | --- |
| `danbooru-zh.csv.gz` | Danbooru 词库，gzip 压缩的 UTF-8 CSV，表头 `tag,zh` |
| `e621-zh.csv.gz` | e621 词库，同格式 |
| `e621-supplement-zh.csv` | 仓库内维护的 e621 专有词汇表（明文 CSV，可直接编辑） |
| `MANIFEST.json` | 生成时间、条目数、SHA-256 与全部数据来源及许可 |

CSV 约定：表头固定为 `tag,zh`；`tag` 为小写下划线形式的标签名，全表按 `tag` 升序排列且不重复；`zh` 为中文译名，多个同义译名以 ` / ` 连接。gzip 容器以 `mtime=0` 写入，因此相同输入会产生逐字节相同的文件。

## 数据来源与许可

具体条目数见 `MANIFEST.json`。合并优先级由高到低：

1. **[amenorira/danbooru-tags-data-zh](https://github.com/amenorira/danbooru-tags-data-zh)** — MIT。人工整理，含别名，目前覆盖画师/作品/元标签。
2. **[GuWuW/danbooru-dict](https://github.com/GuWuW/danbooru-dict)** — 仓库未声明许可。社区词典，每日更新，覆盖常用标签。
3. **[ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table](https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table)** — 仓库未声明许可。覆盖最广（`post_count >= 10`），机器翻译后人工校对。

别名条目指向其规范标签的译名，优先级始终低于规范条目。

e621 没有公开的中文词库，`e621-zh.csv.gz` 由「Danbooru 合并结果 ∩ 已注册 e621 分类快照的标签命名空间」得到，再叠加 `e621-supplement-zh.csv`。后者优先，因为部分标签在两站含义不同（e621 的 `female` 是「雌性」而不是人数）。

> 来源 2、3 的仓库未声明许可。如需对外分发本仓库的构建产物，请自行确认这两个来源的授权是否满足场景，或用 `--sources amenorira` 仅使用 MIT 来源重建词库。

## 重新生成

```powershell
.\runtime\python.exe scripts\build_tag_translations.py                      # 联网抓取全部来源
.\runtime\python.exe scripts\build_tag_translations.py --offline            # 只用缓存目录中已下载的文件
.\runtime\python.exe scripts\build_tag_translations.py --sources amenorira  # 仅使用 MIT 来源
```

下载缓存默认位于 `.tmp-tag-translations/`（已被 `.gitignore` 忽略）。e621 词库的生成依赖已注册的 e621 分类快照资源；若没有注册，脚本会退回到完整的 Danbooru 表并打印告警。

补充或纠正 e621 译名时直接编辑 `e621-supplement-zh.csv`（表头 `tag,zh`），然后重新运行脚本。
