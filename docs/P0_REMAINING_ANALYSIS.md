# P0 remaining analysis

This file replaces the early WIP diagnosis. The baseline control-plane fixes
are now implemented; the remaining work is production-scale validation and
deeper orchestration rather than missing API primitives.

## Delivered

- V1/V2 configuration migration, strict public `json | txt | both` export
  values, resource category checks and content-addressed snapshots.
- Import-time sample registration, durable issues/reviews/overlays, and
  review-gated commit.
- Explicit Start, CAS lifecycle transitions, pause/cancel/resume/recover,
  startup interruption recovery, normalized dataset locks and durable events.
- Atomic in-place backup/Restore controls, full-copy Restore rejection,
  Discard and pin retention.
- Isolated OCR runtime checks, OCR sidecar fingerprints, NL OCR payload wiring,
  and configured NL model override.
- Scoped operation idempotency, concurrent commit-journal allocation and
  v2/v3 migration data preservation.

## Remaining release work

1. Split the aggregate pipeline stage into Import/Caption/Classify/Replace/OCR/NL/
   Review/Export stage runs with 500-sample checkpoints and durable leases.
2. Run a real e621 profile with explicitly imported classification snapshot,
   replacement index, tokenizer and CPU OCR runtime. Missing resources must be
   reported as `blocked_resource`, never replaced with mocks.
3. Add 100k control-plane pressure, restart, disk-full, DB-lock, worker-hang,
   SSRF, hash-drift, path-traversal and commit/restore power-loss tests.
4. Add baseline-drift checks and durable per-file undo/artifact records to the
   commit/Restore transaction boundary; stream large files instead of loading
   them as one `bytes` object.
5. Upgrade the Dataset Workflow event-cursor polling adapter to long-lived SSE
   where deployment authentication/proxy constraints permit it.
6. Re-run full repository mypy after legacy non-workflow errors are addressed;
   changed workflow modules already pass mypy and Ruff.

## Verification snapshot

- Backend: `402 passed, 2 skipped`.
- Frontend: `38 passed`; ESLint and TypeScript/Vite build pass.
- Workflow mypy/Ruff: pass in `.venv-dev` tool environment.
- Port guard: `17 passed`.
- Real smoke: five random image+JSON pairs from `E:\琥珀训练集预备`, 15 files
  exported, zero failures/issues; source directory remained read-only.
