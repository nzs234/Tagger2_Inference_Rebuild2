# Dataset Workflow P0 progress

Updated 2026-08-13. This report supersedes the earlier WIP snapshot that
described Count Review, lifecycle controls, Restore, and sample registration as
unimplemented.

## Delivered in the current checkpoint

- API job creation is `pending`; explicit `POST /jobs/{id}/start` acquires the
  authoritative normalized dataset lock and queues execution.
- Pause/cancel/recover/resume, startup interruption recovery, CAS transitions,
  durable events, pin/unpin, Restore and Discard controls are wired.
- Import registers `workflow_samples`; issues, reviews, resource snapshots,
  stage runs, operations, artifacts and commit journal rows are persisted.
- Count/Token review results are written to workspace overlays and are gates
  before target dataset commit.
- V1 configuration is deterministically readable/migrated to V2; public export
  values are `json | txt | both`; resource IDs are content-verified.
- OCR is fail-closed on the isolated runtime and records cache fingerprints;
  NL receives the OCR sidecar and supports the configured model override.
- DatasetWorkflow has explicit Start, lifecycle controls, pin retention and a
  replayable event-cursor panel with AbortSignal/generation isolation.
- DB v2/v3 migration preserves jobs, samples, issues, reviews and operation
  history; legacy global operation idempotency is rebuilt to a scoped index.

## Verification

- Backend: `420 passed, 1 skipped`.
- Frontend: `39 passed`; ESLint and TypeScript/Vite build pass.
- Full repository mypy gate: pass (`72` source files across backend and scripts).
- Explicit Ruff gate (`E4`, `E7`, `E9`, `F`): pass.
- Source algorithm port guard: `17 passed`.
- Updated real/API smoke: 20 randomly selected image+JSON pairs from the
  user-provided training-set folder completed `pending -> queued -> completed`;
  60 files were exported with zero failures/issues. Legacy `{tag,nl}` sidecars
  were normalized to canonical `tags`; all 20 output JSON files had non-empty
  tags. The source directory remained read-only.
- Real-material smoke: 5 randomly sampled image+JSON pairs from the user-provided
  training-set folder exported 15 files with `failed=0` and no issues. The
  source directory was read-only; samples were copied to a temporary directory.

## Release blockers still open

- `.venv-dev` remains an optional developer environment; the project runtime
  and lock remain authoritative for the release gates.
- Local e621 resources are provisioned and digest-verified: classification
  snapshot `classify-e621-20260812-v1`, replacement index
  `replace-e621-pass-drop-v2`, Qwen tokenizer
  `tokenizer-qwen3-0-6b-tokenizer-v1`, and CPU PaddleOCR descriptor
  `ocr-paddleocr-2-9-1-cpu-v1`. The resource bytes remain outside Git under
  ignored `data/`; setup/import commands are documented in `README.md`.
- Stage-run persistence now covers pipeline/import/caption/classify/replace/
  OCR/NL/policy/token/export/review with checkpoints; finer per-batch
  orchestration, long-lived SSE, pressure/chaos tests, and GPU-specific
  acceptance remain future work.
- Pressure/chaos testing was intentionally skipped for this delivery pass per
  request; only the small real-material flow gate was required.

## Resource acceptance update

- Random 20-image smoke with all four resources enabled: `20/20` exported,
  `20/20` OCR processed, `20/20` token counts within budget, no issues.
- FastAPI resource smoke: `pending -> queued -> completed`, 20 images,
  classification/replacement/tokenizer/OCR resource fingerprints persisted in
  the report; target output contained three files and no source mutation.
- The full repository mypy gate is now clean; the four byte-comparable source
  ports are covered by a scoped per-module diagnostic override so their source
  text remains byte-identical.
