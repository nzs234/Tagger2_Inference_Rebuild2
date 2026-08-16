# P0 remaining analysis

This file records the remaining production-scale work after the workflow control-plane checkpoint.

## Delivered

- V1/V2 configuration migration, strict public `json | txt | both` export values, resource category checks and content-addressed snapshots.
- Import-time sample registration, durable issues/reviews/overlays, and review-gated commit.
- Explicit Start, CAS lifecycle transitions, pause/cancel/resume/recover, startup interruption recovery, normalized dataset locks and durable events.
- Atomic in-place backup/Restore controls, full-copy Restore rejection, Discard and pin retention.
- Isolated OCR runtime checks, OCR sidecar fingerprints, NL OCR payload wiring, and configured NL model override.
- Scoped operation idempotency, concurrent commit-journal allocation and v2/v3 migration data preservation.
- Pipeline/import/export/review stage runs with checkpoints; stage cleanup closes unexpected `running` rows.
- Legacy `{tag,nl}` annotation sidecars are normalized into canonical `tags`/`nl` fields.

## Remaining release work

1. Run the remaining 5k/100k pressure and chaos matrix. The execution path now
   claims, heartbeats and releases durable leases in 500-sample batches and
   resumes from immutable projection and per-file staging checkpoints.
2. Keep the real e621 profile resources explicitly provisioned and re-verify
   their digests when the catalog is refreshed. Missing resources must be
   reported as `blocked_resource`, never replaced with mocks.
3. Add 100k control-plane pressure, restart, disk-full, DB-lock, worker-hang, SSRF, hash-drift, path-traversal and commit/restore power-loss tests.
4. Add durable per-file undo/artifact records to the commit/Restore transaction boundary. Baseline-drift checks and streaming large-file copy/restore are now implemented; power-loss recovery coverage remains follow-up work.
5. Keep the JSON event cursor as a fallback; Dataset Workflow now prefers the
   authenticated long-lived SSE stream with `Last-Event-ID` and heartbeats.
6. Keep the full repository mypy/Ruff gates in the release checklist. The
   current runtime reports both clean; immutable source ports use only scoped
   mypy diagnostic overrides to preserve byte-level parity.

## Verification snapshot

The release script generates the current gate counts. It includes the source
port check, workflow review/restart smoke, backend/frontend suites and static
analysis. Large-scale and fault-injection acceptance remains intentionally
separate from the deterministic developer gate.
