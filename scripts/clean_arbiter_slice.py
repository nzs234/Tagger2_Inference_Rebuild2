"""Strip explicit-content material from a GLM arbitration slice.

The GLM content filter refuses requests whose context carries explicit
e621/danbooru prose, which kills a whole slice's subagent. The arbiter
design tolerates GLM absence (two-party adoption), so the fix is to
exclude the risky entries from the slice entirely: titles matching the
entry blocklist, and entries that lose too many blocklist lines from
their text. Excluded entries are appended to ``excluded_glm.jsonl``
next to the slices and are still judged by the Gemma and CPA judges.

Usage::

    python scripts/clean_arbiter_slice.py data/tag_wiki/arbiter_glm/slices/slice_08.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ENTRY_BLOCK = re.compile(
    r"penis|vulva|vagina|scrotum|cum|semen|anal|masturbat|genital|nude|nudity|porn|hentai|fetish|rape|erection|"
    r"orgasm|clit|foreskin|smegma|urinat|defecat|feces|menstruat|lactat|testicle|castrat|zoophilia|bestialit|"
    r"incest|pedophil|loli|shota|cub|oppai|sheath|knotted|penetrat|intercourse|blowjob|handjob|paizuri|footjob|"
    r"rimjob|pegging|gangbang|bukkake|ahegao|mastur|erect|horny|sexy|sex_|_sex|^sex$|bondage|bdsm|dominat|submissive|"
    r"阴茎|阴户|阴道|肛门|精液|口交|乳交|手淫|自慰|强奸|乱伦|性器|勃起|肛交|性交|做爱",
    re.IGNORECASE,
)
LINE_BLOCK = re.compile(
    r"penis|vulva|vagina|scrotum|\bcum\b|semen|anal|masturbat|genital|nude|nudity|porn|hentai|fetish|rape|erection|"
    r"orgasm|clit|foreskin|smegma|urinat|defecat|feces|menstruat|lactat|testicle|castrat|zoophilia|bestialit|"
    r"incest|pedophil|loli|shota|cub|oppai|sheath|knotted|penetrat|intercourse|blowjob|handjob|paizuri|footjob|"
    r"rimjob|pegging|gangbang|bukkake|ahegao|erect\w*|horny|bondage|bdsm|dominat\w*|submissive|sex\b|sexual",
    re.IGNORECASE,
)


def main() -> None:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    keep, excluded, cleaned = [], [], 0
    for e in data["entries"]:
        # The summaries being judged come from the uncensored local model, so
        # the summary fields themselves can trip the GLM content filter.
        summary_blob = " ".join(str(v) for v in e["summary"].values())
        if ENTRY_BLOCK.search(e["title"]) or ENTRY_BLOCK.search(summary_blob):
            excluded.append(e)
            continue
        lines = e["text"].splitlines()
        kept_lines = [l for l in lines if not LINE_BLOCK.search(l)]
        if len(kept_lines) < 0.4 * len(lines):
            excluded.append(e)
            continue
        if len(kept_lines) != len(lines):
            cleaned += 1
        e["text"] = "\n".join(kept_lines)[:2500]
        keep.append(e)
    data["entries"] = keep
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    excluded_path = path.parent.parent / "excluded_glm.jsonl"
    with open(excluded_path, "a", encoding="utf-8") as f:
        for e in excluded:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(json.dumps({"slice": path.name, "kept": len(keep), "excluded": len(excluded), "text_cleaned": cleaned}))


if __name__ == "__main__":
    main()
