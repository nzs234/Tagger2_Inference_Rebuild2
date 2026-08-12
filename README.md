# Tagger2 Inference Rebuild

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
