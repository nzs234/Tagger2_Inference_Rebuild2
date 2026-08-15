# Dataset Workflow: Compatibility Report

This report records exactly what the merged Dataset Workflow module reproduces
from the source project (`e621-standard-capotion-workflow`), what is not yet
implemented, and where output must differ because a source resource is not
obtainable. It is deliberately blunt: anything not verified is listed as not
verified. Local resources listed below are provisioned outside Git under
`data/workflows/resources`; a fresh checkout must import them before enabling
the corresponding stage.

## 1. Summary

| Area | State |
| --- | --- |
| Nine-field standard JSON + flat TXT | Ported verbatim, tested |
| Replacement engine (keep/replace/drop) | Ported verbatim, tested |
| Designated e621 replacement index | Imported and validated end to end |
| Dataset import (mixed annotation formats) | Implemented, tested |
| Annotation backup / restore / atomic commit | Implemented, tested |
| Offline vertical (import -> replace -> export -> commit) | Implemented, tested |
| Isolated workflow database and resource catalog | Implemented, tested |
| API under `/api/v1/workflows` | Implemented, contract-tested |
| Bilingual Dataset Workflow page | Implemented, tested |
| Caption (adapter onto the host inference engine) | Implemented, tested |
| Classify (official snapshot -> nine fields) | Implemented, tested, e621 snapshot provisioned |
| OCR (isolated PaddleOCR runtime, sidecar output) | Implemented, CPU runtime provisioned and probed |
| NL / Count Review / Policy / Token Budget | Implemented, tested |
| Pause / resume / repair / lease recovery | Implemented, tested |
| Bilingual page incl. OCR controls and per-stage report | Implemented, tested |

The existing product surface is unchanged: `tagger2.sqlite3` (~804 MB) is not
migrated or rewritten, the ~23.5 GB of model assets are not copied, and the
existing `/api/v1/jobs`, models, providers, video-prompt and settings routes
behave as before.

## 2. Behaviour ported verbatim

These files are copied from the source project with only docstring and import
adjustments, so their rules are identical rather than re-derived:

- `backend/tagger2/workflow/caption_format/normalizer.py`
  (from `shared/anima_caption_format/.../normalizer.py`)
- `backend/tagger2/workflow/caption_format/flat_txt.py`
  (from `shared/anima_caption_format/.../flat_txt.py`)
- `backend/tagger2/workflow/stages/replacement.py`
  (from `workers/replace/.../replacement.py`)
- `backend/tagger2/workflow/raw_e621.py`
  (from `core/src/anima_core/raw_e621.py`)

Consequences that are therefore guaranteed, not approximated:

- Output is exactly the nine fields in the frozen order
  `quality, count, character, series, artist, appearance, tags, environment, nl`.
- `count` is restricted to `""`, `solo`, `duo`, `trio`, `group`.
- Cross-field dedup priority is `quality, character, appearance, tags, environment`;
  the first field to emit a tag keeps it.
- Flat TXT joins sections with `", \n\n"`, applies underscore-to-space and
  parenthesis escaping, and terminates with a period.
- Raw e621 grouped JSON must contain exactly the nine e621 groups; `series` is
  left empty for raw e621 input, matching source behaviour.

## 3. Designated replacement index

Source file: `D:\QQ相关\下载\E621tag替换索引\e621_general_tag_replacement_index.csv`

| Property | Value |
| --- | --- |
| Size | 7,418,754 bytes |
| SHA-256 | `4834c1cda2cd560641a7cd67d7cef8d99d381a89f7beab4a86e2ef4f90643ded` |
| Header | `source_tag,canonical_e621_tag,action,replacement_tags` |
| Data rows | 155,706 |
| Duplicate source tags | 0 |

Action breakdown as validated by the importer:

| Action | Rows | Handling |
| --- | --- | --- |
| `keep` | 47,095 | Emit the recorded replacement tag |
| `replace` | 3,171 | Emit the `\|`-separated replacement list (115 rows expand to more than one tag) |
| `drop` | 644 | Remove the tag |
| `pass` | 104,796 | Identity passthrough |

Two findings required decisions, both recorded in code comments and tests:

1. **`pass` is a fourth action** not present in the source project's three-action
   rule set. Every `pass` row in this index has `replacement_tags == source_tag`,
   i.e. pure identity. The importer validates that invariant strictly and then
   omits those rows from the executable rule table, because a tag with no rule
   already passes through unchanged and is counted as passthrough by the ported
   transform. Behaviour is identical while the rule table stays ~104k entries
   smaller. A `pass` row whose replacement differs from its source is a hard
   error, never a silent rewrite.
2. **Whitespace-only source tags are legitimate.** Row 155,705 has `source_tag`
   U+3000 (ideographic space) with a `drop` rule. An initial "blank or padded"
   check wrongly rejected it. The rule now rejects only empty tags, tags padded
   around real content, and tags containing NUL/comma/newline.

Result: the whole index validates cleanly into 50,910 executable rules plus
104,796 identity passthroughs.

## 4. Verified end-to-end behaviour

Confirmed by automated tests plus a 300-sample run against the real index:

- Mixed input in one dataset: bare image, tag TXT, NL TXT, standard JSON and raw
  e621 grouped JSON are each classified correctly.
- Raw e621 JSON and non-blank tag TXT skip the tagger; NL TXT populates `nl` and
  still requires the tagger for classification tags.
- A corrupt raw e621 document raises a blocking issue for that sample and never
  falls back to another path.
- Animated WebP is rejected; unrelated file extensions are skipped, not failed.
- RGBA input is composited onto white rather than silently losing alpha.
- `full_copy` leaves the source dataset byte-identical and copies each
  sample's image alongside its annotation, so the output root is a complete
  dataset rather than annotations without images.
- `in_place` writes a verified ZIP64 backup first, and restoring it returns the
  original bytes and removes files the run created.
- A blocking issue fails closed: nothing is committed and the journal records
  `commit_skipped`.
- A staged file tampered with after validation is refused at commit time.
- The API never returns an absolute server path, and resource import is
  addressed by root id plus relative path through `PathAllowlist`.

Real-index spot check (`male, anthro, watermark, duo_focus, solo, fur, forest`):
`anthro` -> `furry`, `fur` -> `body_fur`, `duo_focus`/`male`/`solo`/`forest` kept,
`watermark` has no rule and passes through. Flat TXT renders as
`male, furry, watermark, duo focus, solo, body fur, forest.`

## 5. Stage status

Every pipeline stage is now built and wired through the API. What remains
unbuilt is listed at the end of this section rather than being implied.

### Classify

Classify reads a `classify-snapshot-v1` resource: one JSON bundle holding the
official tag table, the alias table and the implication table. The published
e621/Danbooru DB exports encode `category` as an integer, so
`build_snapshot_from_official_csv` maps it through a per-profile table and an
unmapped code is an error rather than being folded into `general`. Only `active`
alias rows are kept, matching what the sites themselves apply. Alias chains are
flattened with cycle detection, and validation runs the real rule builder so a
cycle cannot pass a row-level check.

Import with `scripts/import_classification_snapshot.py` (use `--dry-run` to
validate without registering). The snapshot itself is not bundled in Git, but
the current local e621 resource is `classify-e621-20260812-v1` with fingerprint
`eccfdfacf3bcf1611a9ee3561f54bb81e946122f582f1f421c5e90689f2db49f`.

Field mapping is deterministic, never a guess: `character` and `artist` come
from those categories (an artist tag is merged into the `artist` field, not
discarded), `species` fills `appearance`, `rating_*` meta fills `quality`, and
everything else lands in `tags`. `series` stays empty for e621, matching the
frozen source behaviour, so a `copyright` tag is kept in `tags`. `environment`
is only filled by a category that states the distinction, so with the current
official categories it stays empty rather than being populated by a heuristic.

### OCR

OCR is off by default. When enabled, it runs PaddleOCR in a separate interpreter
(`runtime_ocr/`, created by `scripts/setup_ocr_runtime.ps1`) because that
dependency stack conflicts with the main runtime. Results are written to
`<workspace>/ocr_sidecars/<relative_path>.ocr.json` carrying `version: v1`, and
an existing sidecar is reused unless `force_reprocess` is set.

OCR never touches the nine-field payload. Direct/offline stage calls translate
a missing runtime to `ocr_unavailable` and a failed image to `ocr_failed`, both
`severity=warning`, `blocking=false`; production API preflight additionally
requires the registered runtime descriptor and model-cache digest before a job
can start. This is covered by tests asserting the exported JSON keys are
unchanged and that a failing engine still produces a committed file.

The local CPU runtime and its provisioned English det/rec/cls model cache have
also passed a real blank-image probe and the resource smoke; runtime executable
and model-cache fingerprints are checked before execution, so an absent or
drifted cache fails closed without a download.

### Fail-closed profile handling

Preflight refuses, as blocking errors rather than warnings:

- Classify enabled with no snapshot selected.
- A snapshot id that is not registered, or a registered file that cannot be
  parsed.
- A snapshot whose `profile` differs from the job profile, in either direction.
  There is no cross-profile fallback.
- A Danbooru job with Replace enabled but no Danbooru index selected; the e621
  index is not accepted as a substitute.

Selecting the Danbooru profile with the dependent stages off is allowed and
carries a warning that its resources are not bundled. The e621 profile has a
local replacement index (`replace-e621-pass-drop-v2`), Qwen tokenizer
(`tokenizer-qwen3-0-6b-tokenizer-v1`) and PaddleOCR CPU descriptor
(`ocr-paddleocr-2-9-1-cpu-v1`).

### Still not built

- Long-lived SSE event streaming for workflow jobs (the UI currently uses the
  replayable JSON event cursor, with reconnect and generation guards).
- Automated download of the official snapshots. Import is a local, explicit
  step; nothing is fetched over the network on the project's behalf.
- Danbooru-specific replacement and count resources. The profile is selectable
  and fails closed; no substitute data is invented.

## 6. Resources that cannot be reproduced

| Resource | Source scale | Status here |
| --- | --- | --- |
| e621 classification dictionary | Official 2026-08-12 export; 1,596,997 executable tags, 69,819 aliases, 58,603 implications | Provisioned locally as `classify-e621-20260812-v1`; 118 malformed official rows are quarantined and recorded in the anomaly report. |
| e621 wiki count data | Private snapshot | Not obtainable. |
| Source replacement index | 86,923 rules | Designated local artifact has 155,706 rows / 50,910 executable rules and fingerprint `4834c1cda2cd560641a7cd67d7cef8d99d381a89f7beab4a86e2ef4f90643ded`. |
| e621 pass cleanup index | 155,706 executable rules | Provisioned as `replace-e621-pass-drop-v2`: 47,095 keep, 3,171 replace, 105,440 drop, 0 pass; fingerprint `2e3c4af6cc93b7f2cc8e55e2eda024ee69942f08a3618b6c2f0dfe6d45991972`. |
| Tokenizer | Formal tokenizer resource required by the token gate | Qwen3-0.6B tokenizer JSON is provisioned as `tokenizer-qwen3-0-6b-tokenizer-v1`, fingerprint `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`. |
| CPU OCR runtime | Isolated Paddle/PaddleOCR interpreter and model cache | Provisioned locally as `ocr-paddleocr-2-9-1-cpu-v1`; runtime and model-cache drift are checked before execution. |
| `lse14-scorer-5k-v1` | 5k quality scorer | Not obtainable. The existing LSE14 1k asset may be used only when explicitly labelled; it is never presented as the 5k model. |
| Danbooru formal resources | Private | Not obtainable. The profile is selectable but preflight warns, and the stage must fail closed rather than fall back to e621. |

Because of the above, byte-for-byte reproduction of the source project's
end-to-end output is not achievable. What is achievable, and is delivered, is
identical behaviour for the rule-only stages and an explicit, visible record of
every difference.

## 7. Verification commands

```powershell
# Backend (project-internal runtime)
.\runtime\python.exe -m pytest backend\tests -q

# Frontend
cd frontend
npm run test
npm run lint
npm run build
```

Current results (2026-08-14): backend 416 passed / 1 skipped; frontend 41
passed; changed workflow modules pass targeted mypy and Ruff checks. The
20-image real-resource smoke completed with 20/20 samples and no issues.
Ruff clean; frontend lint and `tsc -b` clean. The real local-model/resource
smoke completed a one-image Caption + Classify + Replace + OCR + Tokenizer run
and a separate 20-image offline/API run without blocking issues. Stress and
GPU-specific gates are intentionally not claimed here.

Note on running the backend suite: use the project runtime
(`.\runtime\python.exe`). It puts only `backend` on `sys.path`, so test modules
must import as `tagger2.*` at module level. A `backend.tagger2.*` import at
module level passes under a system Python and fails under the project runtime.

`backend/tests/test_workflow_ports.py` asserts that each verbatim-ported file is
still byte-identical to its source counterpart (skipped automatically when the
source project is not present). Two `ruff` unused-import warnings remain inside
those ported files; they exist in the originals and are left alone so the ports
stay byte-comparable.
