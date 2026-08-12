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

- Backend: `407 passed, 1 skipped`.
- Frontend: `38 passed`; ESLint and TypeScript/Vite build pass.
- Workflow mypy gate (the changed workflow modules): pass.
- Explicit Ruff gate (`E4`, `E7`, `E9`, `F`): pass.
- Source algorithm port guard: `17 passed`.
- Updated real/API smoke: 20 randomly selected image+JSON pairs from the
  user-provided training-set folder completed `pending -> queued -> completed`;
  60 files were exported with zero failures/issues. Legacy `{tag,nl}` sidecars
  were normalized to canonical `tags`; all 20 output JSON files had non-empty
  tags. The source directory remained read-only.
- Real-material smoke: 5 randomly sampled image+JSON pairs from
  `E:\琥珀训练集预备` exported 15 files with `failed=0` and no issues. The
  source directory was read-only; samples were copied to a temporary directory.

## Release blockers still open

- A fully populated `.venv-dev` could not be installed within the local time
  limit; the lock remains authoritative and the isolated Ruff/mypy tools pass.
- Full repository mypy outside the changed workflow modules still has legacy
  errors; this is not claimed as a release-gate pass.
- Real e621 classification snapshot, replacement index, tokenizer and CPU OCR
  runtime are not bundled. Missing resources must report `blocked_resource`.
- Stage-run persistence currently covers pipeline/import/export/review with
  checkpoints; finer per-module/per-batch orchestration, long-lived SSE,
  pressure/chaos tests, and real OCR/GPU acceptance remain future work.
- Pressure/chaos testing was intentionally skipped for this delivery pass per
  request; only the small real-material flow gate was required.
