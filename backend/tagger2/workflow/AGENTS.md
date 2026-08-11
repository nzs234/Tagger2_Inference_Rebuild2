# Dataset Workflow module conventions

Scope: `backend/tagger2/workflow/`.

## Ported code is verbatim

`caption_format/normalizer.py`, `caption_format/flat_txt.py`,
`stages/replacement.py` and `raw_e621.py` are copied from the
`e621-standard-capotion-workflow` project. Keep them byte-comparable: adjust
callers rather than these algorithms, so rule-stage output stays identical to the
source project. Each file carries a header naming its origin.

## Hard rules

- Output is exactly the nine fields, in order: `quality`, `count`, `character`,
  `series`, `artist`, `appearance`, `tags`, `environment`, `nl`.
- `count` is one of `""`, `solo`, `duo`, `trio`, `group`.
- Paths cross the API as `{root_id, relative_path}` and resolve through
  `PathAllowlist`. Never return an absolute server path and never accept one.
- Never write to the dataset before a verified backup exists in `in_place` mode.
- A blocking issue must prevent the commit; never leave a half-written dataset.
- A missing resource makes its stage unavailable. Never fall back to a different
  resource, profile or model silently.
- Never repair malformed input. Report the offending line or sample and stop.

## Boundaries

- The workflow database is `data/workflows/workflows.sqlite3`. Do not read or
  write `tagger2.sqlite3` from this module.
- New stages must stage their output through `ExportStaging` and commit through
  `commit_staged_files` so recovery and rollback keep working.
- Reuse the host capabilities instead of duplicating them: `LocalInferenceEngine`
  and `ModelRegistry` for caption, the provider clients and `SecretStore` for NL,
  `PathAllowlist` for path safety.

## Tests

Run with the project runtime, which has the dependencies installed:

```powershell
.\runtime\python.exe -m pytest backend\tests -q
```

Every behavioural claim needs a test. When a real input file drives a decision
(for example the `pass` action or whitespace-only tags in the designated e621
index), encode that case as a test and record it in
`docs/workflow_compatibility_report.md`.
