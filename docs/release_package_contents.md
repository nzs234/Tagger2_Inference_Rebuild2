# Tagger2 发布包说明

这个压缩包用于分享和部署 Tagger2 Inference。V1.04.1 基础 Python 包内置便携式 Python
3.12，但不包含第三方 site-packages；解压后运行 `setup.bat`，脚本会在该 runtime 中安装
锁定依赖，之后运行 `start.bat`，浏览器打开 `http://127.0.0.1:20000`。也可以在启动前
设置 `TAGGER2_TORCH_VARIANT=cpu` 强制使用 CPU。

## 已包含

- Tagger2 后端、前端构建产物、启动/安装脚本和锁定的依赖清单。
- 原生多供应商图像生成页面，支持 Gemini / Nano Banana、OpenAI GPT Image、
  xAI Grok Image 与兼容线路；图像任务数据库和产物目录在首次使用时创建。
- `update.bat`：用于 Git 检出目录的安全 fast-forward；发行 ZIP 本身不包含 `.git`，
  ZIP 用户应从 GitHub Releases 下载新版并迁移本地配置、模型、缓存和数据目录。
- 数据集工作流资源 **manifests**（仅清单，模型类大文件不随包）：
  - 分类快照：`classify-e621-20260812-v1`（e621）、`classify-danbooru-20260902-v1`（danbooru）
  - Tokenizer：`tokenizer-qwen3-0-6b-tokenizer-v1`
- e621 标签替换索引（数据表，随包）：
  - `e621-replacement-index-v1`（旧版，保留兼容性）
  - `replace-e621-index-v1`（旧版，保留兼容性）
  - `replace-e621-pass-drop-v2`（推荐；原 `pass` 全部转换为 `drop`）
    - fingerprint：`2e3c4af6cc93b7f2cc8e55e2eda024ee69942f08a3618b6c2f0dfe6d45991972`
    - 规则统计：keep 47,095、replace 3,171、drop 105,440、pass 0

自 V1.10.1 起，构建完成的 **Tag Wiki 数据库随包发行**（`data/tag_wiki/`）：

- `tag_wiki.sqlite3`（e621 镜像：页面 / 章节 / 向量 / 中文摘要）
- `tag_wiki_danbooru.sqlite3`（danbooru 镜像，同 schema）

打包时通过 SQLite `VACUUM INTO` 生成干净紧凑的快照（scripts/snapshot_wiki_databases.py），
解压即用、无需重建语料；跨语言嵌入模型本身仍属模型类大文件，首次构建时按需下载。

资源位于 `data/workflows/resources/`，每个资源都带有独立 manifest 和内容指纹。
分类快照与 tokenizer 属模型类大文件（合计约 131 MB），自 V1.10 起不随包发行：首次使用时应用自动从
[resources-v1 发行页](https://github.com/nzs234/Tagger2_Inference_Rebuild2/releases/tag/resources-v1)
按指纹下载校验（也可提前运行 `runtime\python.exe scripts\fetch_workflow_resources.py` 预取）。
完全离线部署可手动把对应内容寻址文件放入 `data/workflows/resources/<category>/`。

## 未包含

- `models/` 和 `data_cache/`：模型文件体积较大，需要在本机单独准备。模型配置仍使用
  Tagger2 的本地模型页面导入/选择，任务会冻结明确的 `caption.model_id`。
- 基础 Python 包包含 `runtime/python.exe`，但不包含第三方依赖；首次运行 `setup.bat`
  会在包内 runtime 安装锁定依赖。
- `runtime_ocr/`、PaddleOCR 模型缓存和 OCR 资源描述：这些文件体积较大，且描述中可能含
  原机器的绝对路径。需要 OCR 时，请按项目文档单独安装隔离 OCR 运行时并注册本机资源。

V1.04.1 延续 V1.03 的上游固定基线 `ccc9d07497be637fc097c5da009d791f017144c9`。Replacement
保留上游的随机 `anthro` → `furry` 规则；调用方按 `job_id + sample_id +
relative_path` 注入任务内稳定 seed，因此同一任务的审阅恢复不会改变已冻结的输出。

## 第一次使用

1. 解压到不含特殊权限限制的目录。
2. 准备至少一个本地 Caption 模型，并在“本地模型”页面确认模型可用。
3. 在“数据集工作流”中手动填写源数据集和输出目录，运行“检查设置”，再创建并显式开始任务。
4. 默认 e621 任务使用 `replace-e621-pass-drop-v2`；旧作业或明确选择旧资源的任务不会被改写。

发布包不包含用户数据、数据库、日志、访问令牌或密钥。

`VERSION.txt` 和 `VALIDATION_REPORT.json` 会记录发行版本、源码提交、构建时间及固定上游提交；
打包脚本拒绝存在未提交修改的工作树。

V1.04.1 发布验收包含后端完整测试、前端单元测试、Playwright、Ruff、mypy、
ESLint、TypeScript/Vite、固定上游端口校验和 workflow smoke；本地资源未准备时，
资源 smoke 会明确报告 blocked_resource，而不会以 mock 或其他资源替代。

基础 Python 发行包还会验证 `get-pip.py` 存在、第三方依赖目录未被误打包、旧运行时
marker 已被清除。`setup.bat` 和 `start.bat` 都按 pip 的实际可用状态执行引导。
