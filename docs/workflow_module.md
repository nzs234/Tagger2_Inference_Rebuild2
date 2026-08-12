# Dataset Workflow Integration

Merges the dataset annotation pipeline from `e621-standard-capotion-workflow`
into this project as a self-contained **Dataset Workflow** module, without
disturbing the existing Workbench, Batch, Models, Providers, Settings or Video
Prompts features.

For exactly what is reproduced, what is missing, and why, see
[workflow_compatibility_report.md](workflow_compatibility_report.md).

## Layout

```
backend/tagger2/workflow/
  contracts.py           versioned job config, path refs, resource manifests
  db_schema.py           schema v1 for the isolated workflows database
  db.py                  connection management, job/sample/issue operations
  resources.py           content-addressed resource catalog, per-category readers
  replacement_index.py   strict reader for the e621 replacement index CSV
  classify_snapshot.py   strict reader/builder for classify-snapshot-v1 bundles
  raw_e621.py            ported strict raw e621 grouped JSON parser
  dataset_import.py      dataset scan and annotation-format classification
  ocr.py                 isolated PaddleOCR runtime and sidecar protocol
  commit.py              annotation backup, staging, journal, atomic commit
  pipeline.py            offline vertical orchestration
  preflight.py           configuration validation and fail-closed profile checks
  lifecycle.py           pause / resume / repair / lease recovery
  count_review.py        count decisions and the export gate
  token_budget_review.py overflow review and the export gate
  api.py                 router mounted at /api/v1/workflows
  caption_format/        ported nine-field normalizer and flat TXT serializer
  stages/                caption, classify, replacement, nl, policy, token budget

frontend/src/
  pages/DatasetWorkflow.tsx   the module page
  lib/workflowCopy.ts         bilingual copy table
```

## Design boundaries

**Separate database.** The module owns `data/workflows/workflows.sqlite3` in WAL
mode with its own migration version. The existing `tagger2.sqlite3` is never
migrated or rewritten.

**Separate resource library.** Resources live under
`data/workflows/resources/<category>/`, content-addressed by SHA-256 with a
manifest recording provenance. Model assets are referenced, never copied. The
category selects the validating reader: `classify` reads a
`classify-snapshot-v1` bundle, `replace` / `replacement_index` read the index
CSV, `tokenizer` probes a serialized `tokenizer.json`, and `ocr` validates an
isolated CPU PaddleOCR runtime descriptor. An unknown category is refused
rather than guessed at.

**Per-job workspace.** `data/workflows/jobs/<job-id>/` holds the immutable input
manifest, the frozen config snapshot, the staging tree, the issue log, the
annotation backup and the commit journal.

**Paths never leak.** Every path crosses the API as `{root_id, relative_path}`
and is resolved through the existing `PathAllowlist`. Responses contain no
absolute server paths, and resource import cannot name an arbitrary file.

**Fail closed.** A blocking issue prevents the commit entirely rather than
producing a partially written dataset. Missing resources make a stage
unavailable; they never trigger a silent fallback.

**In-place safety.** `in_place` mode always writes a verified ZIP64 backup of the
original `.txt` / `.json` annotations before the first dataset write, and that
backup can restore the original bytes.

## Bilingual scope

Only this module is bilingual. `workflowCopy.ts` is typed as
`Record<WorkflowLanguage, WorkflowCopy>`, so both languages must define every
key. The default is Chinese and the choice persists in the existing preferences
store. The rest of the application remains Chinese.

## Working on this module

- Run the backend suite with the project runtime:
  `.\runtime\python.exe -m pytest backend\tests -q`
- That runtime puts only `backend` on `sys.path`. Import as `tagger2.*` at module
  level in tests; a module-level `backend.tagger2.*` import passes under a system
  Python and fails under the project runtime.
- OCR needs a separate interpreter and provisioned det/rec/cls model cache.
  Build it with `.\scripts\setup_ocr_runtime.ps1`, then register its descriptor
  with `scripts/import_ocr_runtime_resource.py`; runtime or model digest drift
  blocks a job before any sample is touched.
- Register a classification snapshot with
  `scripts/import_classification_snapshot.py --dry-run` first, then without it.
- Register the Qwen tokenizer with `scripts/import_tokenizer_resource.py` and
  the designated replacement index with
  `scripts/import_designated_replacement_index.py`.
- Keep ported files verbatim. If a source algorithm needs changing, change the
  caller instead, so rule-stage output stays comparable to the source project.
- Any new stage must write into the workspace staging tree and commit through
  `commit_staged_files`, so recovery and rollback keep working.
- Record every behavioural difference from the source project in the
  compatibility report.
