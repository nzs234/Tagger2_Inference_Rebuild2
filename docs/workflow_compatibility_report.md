# Dataset Workflow: Compatibility Report

This report records exactly what the merged Dataset Workflow module reproduces
from the source project (`e621-standard-capotion-workflow`), what is not yet
implemented, and where output must differ because a source resource is not
obtainable. It is deliberately blunt: anything not verified is listed as not
verified.

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
| Caption / Classify / OCR / NL / Count Review / Policy / Token Budget | Not implemented yet |
| Pause / resume / repair / lease recovery | Not implemented yet |

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
- `full_copy` leaves the source dataset byte-identical.
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

## 5. Not implemented yet

These stages are designed for but not built. They are absent, not stubbed to
look successful:

- Caption (adapter onto the existing `LocalInferenceEngine` and `ModelRegistry`)
- Classify (e621 and Danbooru dictionaries)
- OCR (isolated PaddleOCR runtime and versioned sidecar protocol)
- NL (adapter onto the existing provider clients and `SecretStore`)
- Count Review (single, batch and confirm flows)
- Dropout / Policy (directory-name-to-artist, seeded dropout)
- Token Budget (tokenizer counting and overflow review)
- Pause / resume / repair / lease expiry / discard / retention
- SSE event stream for workflow jobs

The job record and issue tables already carry the columns these stages need, and
the workspace layout, staging tree and commit journal are stage-agnostic.

## 6. Resources that cannot be reproduced

| Resource | Source scale | Status here |
| --- | --- | --- |
| e621 classification dictionary | 120,978 audited entries | Not obtainable. Classify stays unavailable; no substitute dictionary is invented. |
| e621 wiki count data | Private snapshot | Not obtainable. |
| Source replacement index | 86,923 rules | Different artifact. The designated index supplied here has 155,706 rows / 50,910 executable rules and its own resource id and fingerprint. |
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

Current results: backend 136 passed / 1 skipped; frontend 20 passed
(13 pre-existing plus 7 new); frontend lint clean; production build succeeds.

`backend/tests/test_workflow_ports.py` asserts that each verbatim-ported file is
still byte-identical to its source counterpart (skipped automatically when the
source project is not present). Two `ruff` unused-import warnings remain inside
those ported files; they exist in the originals and are left alone so the ports
stay byte-comparable.
