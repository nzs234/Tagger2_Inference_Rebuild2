# Tagger2 Inference Rebuild

**V1.03 · Windows 本地优先的图像打标与数据集工作流工作台**

Tagger2 Inference Rebuild 将本地 Caption 模型推理、单图/批量任务、e621 标签整理、确定性标签替换、可选 OCR、数量复核、Token 检查和 JSON/TXT 导出统一到一个可复现的 FastAPI + React 应用中。原项目仓库保持不变，本仓库是独立重建版本。

## 核心能力

- 使用本地 Caption 模型进行单图和批量图片打标。
- 通过 e621 分类快照整理标签，并使用不可变替换索引。
- 支持生成新数据集或更新原数据集；原地更新会先创建备份。
- 在导出前执行数量复核和 Token 长度检查，复核未确认时不会提交目标数据集。
- 支持暂停、恢复、取消、恢复任务、事件查看和失败回滚。
- 所有工作流资源通过 manifest、大小和 SHA-256 指纹冻结。

## 三分钟了解工作流

1. 启动服务并打开 `http://127.0.0.1:20000`。
2. 在“本地模型”页面确认 Caption 模型已经加载。
3. 在“数据集工作流”中手动填写源数据集和输出目录。
4. 选择 e621 分类、替换、OCR 和 Token 处理步骤。
5. 点击“检查设置”，预检通过后创建任务。
6. 创建后再显式点击“开始处理”，完成复核后导出结果。

默认推荐的 e621 替换索引为 `replace-e621-pass-drop-v2`，其中原索引的 `pass` 已全部转换为 `drop`（keep 47,095、replace 3,171、drop 105,440、pass 0）。

模型权重、用户图片、数据库和 OCR 模型缓存不会提交到 Git 仓库或发布包；模型和机器相关运行时需要在本机单独准备。详细安装、资源导入和发布说明见下文及 [`docs/release_package_contents.md`](docs/release_package_contents.md)。

This is the independent local/online image-tagging workspace. The original
project is left untouched. The server targets Windows 10/11, Python 3.12 and
CUDA with a CPU fallback. Node is only needed when rebuilding the frontend;
released packages serve the existing `frontend/dist` files directly.

## Quick start

Portable releases include Python 3.12. Run `setup.bat` on a new computer; it
detects NVIDIA hardware, installs the matching locked CUDA or CPU runtime, and
then starts the application. Internet access is required on the first run.

Run `start.bat`. It creates `.venv`, selects CUDA when `nvidia-smi` is
available (override with `TAGGER2_TORCH_VARIANT=cpu` or `cuda`), installs the
matching ML wheels before the web dependencies, and starts the API at
`http://127.0.0.1:20000`.

For a manual setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-gpu.lock
# Use requirements-cpu.lock instead on a CPU-only machine.
cd frontend
npm ci
npm run build
cd ..
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python.exe -m tagger2.main
```

Copy `config/app.example.toml` to `config/app.toml` for a new deployment.
The checked-in profile is safe to edit and contains no credentials. TOML
values are loaded at startup; `TAGGER2_*` environment variables take
precedence. LAN binding requires `lan_access = true` and a token in the
environment variable named by `access_token_env`.

### Dataset Workflow resources

The first e621 profile requires four local, content-addressed resources. They
are kept under the ignored `data/workflows/resources` directory and must be
provisioned explicitly; the workflow never guesses a resource or downloads one
during a job.

```powershell
# Official e621 exports (the command accepts .csv.gz directly).
.\runtime\python.exe scripts\import_classification_snapshot.py `
  --profile e621 `
  --tags-csv C:\snapshots\e621\tags.csv.gz `
  --aliases-csv C:\snapshots\e621\tag_aliases.csv.gz `
  --implications-csv C:\snapshots\e621\tag_implications.csv.gz `
  --resource-id classify-e621-20260812-v1 `
  --source-url https://e621.net/db_export/ `
  --allow-official-anomalies `
  --anomaly-report C:\snapshots\e621\quarantine.json

# A serialized Qwen3 tokenizer.json (weights are not required for counting).
.\runtime\python.exe scripts\import_tokenizer_resource.py `
  C:\snapshots\qwen3-0.6b\tokenizer.json `
  --source-url https://huggingface.co/Qwen/Qwen3-0.6B/resolve/main/tokenizer.json

# Isolated CPU OCR runtime and its provisioned det/rec/cls model cache.
powershell -ExecutionPolicy Bypass -File .\scripts\setup_ocr_runtime.ps1
.\runtime_ocr\Scripts\python.exe scripts\import_ocr_runtime_resource.py
```

The official e621 export currently contains a small number of malformed tag
names. `--allow-official-anomalies` does not repair them: it quarantines the
exact source rows in the requested report and records the count in snapshot
metadata. Omitting the flag keeps the importer strict and fails closed. The
designated replacement index is imported with
`scripts/import_designated_replacement_index.py`.

For the e621 production default, the immutable local resource
`replace-e621-pass-drop-v2` is used. It changes only `action=pass` rows to
`action=drop` and clears their `replacement_tags` (47,095 keep / 3,171
replace / 105,440 drop / 0 pass). The original V1 resources remain available
for old jobs and reproducibility.

Runtime lock files pin every direct and transitive dependency with SHA-256
hashes. The `.txt` files are source manifests, not deployment inputs. Update
all locks on Python 3.12 with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\compile_requirements.ps1
```

Use `-Target cpu`, `gpu`, or `dev` to update one lock. Lock validation rejects
editable requirements, local paths, missing hashes, and non-exact versions.

## LSE14 aesthetic scoring

The optional local aesthetic classifier uses the official
[`lse14/lse14-scorer`](https://huggingface.co/lse14/lse14-scorer)
`1k.safetensors` checkpoint and its five-point scoring formula. Results include
the overall 1-5 score and bucket, composition, color, sexual-content score, and
in-domain probability. Style clustering is not part of the rebuilt runtime.

The first classifier load may download the fixed SigLIP and OpenAI CLIP
backbones plus the LSE14 head. Assets are stored under `models` and
`data_cache/huggingface` for later offline reuse. Set `HF_TOKEN` in the process
environment when Hugging Face authentication is required; tokens are never
written to the project configuration or included in release packages.

## Benchmark and release

The benchmark accepts an opaque model ID, display name or model directory and
uses the current model registry and batched inference API:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark.py `
  --model SmilingWolf__wd-eva02-large-tagger-v3 `
  --images data\uploads --device cuda --batch-size 16 --limit 100
```

Run the read-only workbench smoke test to verify one image with one and two
local models. It uses preset model thresholds and does not create jobs or
artifacts:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_workbench_local.py --device cuda
```

Build a source-plus-frontend release (models and caches are provisioned
separately):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

The release command scans out credentials and runs an extracted-package
health smoke test. API keys and Hugging Face tokens from the old project are
never migrated; rotate any key found in old configuration before use.
