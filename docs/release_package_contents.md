# Tagger2 发布包说明

这个压缩包用于分享和部署 Tagger2 Inference。当前 GitHub V1.01 Release 提供的是
精简包：不内置 Python 运行时，解压后运行 `setup.bat`，脚本会下载 Python 3.12 并安装
锁定依赖；之后运行 `start.bat`，浏览器打开 `http://127.0.0.1:20000`。也可以在启动前
设置 `TAGGER2_TORCH_VARIANT=cpu` 强制使用 CPU。包含 `runtime/` 目录的本地完整包则可以
直接跳过 Python 下载步骤。

## 已包含

- Tagger2 后端、前端构建产物、启动/安装脚本和锁定的依赖清单。
- e621 分类快照：`classify-e621-20260812-v1`
  - fingerprint：`eccfdfacf3bcf1611a9ee3561f54bb81e946122f582f1f421c5e90689f2db49f`
- e621 标签替换索引：
  - `e621-replacement-index-v1`（旧版，保留兼容性）
  - `replace-e621-index-v1`（旧版，保留兼容性）
  - `replace-e621-pass-drop-v2`（推荐；原 `pass` 全部转换为 `drop`）
    - fingerprint：`2e3c4af6cc93b7f2cc8e55e2eda024ee69942f08a3618b6c2f0dfe6d45991972`
    - 规则统计：keep 47,095、replace 3,171、drop 105,440、pass 0
- Tokenizer：`tokenizer-qwen3-0-6b-tokenizer-v1`
  - fingerprint：`aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`

资源位于 `data/workflows/resources/`，每个资源都带有独立 manifest 和内容指纹。

## 未包含

- `models/` 和 `data_cache/`：模型文件体积较大，需要在本机单独准备。模型配置仍使用
  Tagger2 的本地模型页面导入/选择，任务会冻结明确的 `caption.model_id`。
- 精简包不包含 `runtime/`；首次运行 `setup.bat` 会自动准备便携 Python 和依赖。
- `runtime_ocr/`、PaddleOCR 模型缓存和 OCR 资源描述：这些文件体积较大，且描述中可能含
  原机器的绝对路径。需要 OCR 时，请按项目文档单独安装隔离 OCR 运行时并注册本机资源。

## 第一次使用

1. 解压到不含特殊权限限制的目录。
2. 准备至少一个本地 Caption 模型，并在“本地模型”页面确认模型可用。
3. 在“数据集工作流”中手动填写源数据集和输出目录，运行“检查设置”，再创建并显式开始任务。
4. 默认 e621 任务使用 `replace-e621-pass-drop-v2`；旧作业或明确选择旧资源的任务不会被改写。

发布包不包含用户数据、数据库、日志、访问令牌或密钥。
