"""Merge the three independent re-judge verdict sets and apply a 2-of-3 quorum.

Parties and their result files:

- gemma (local LM Studio) — ``data/tag_wiki/verify_results.jsonl``
- cpa (Gemini proxy)      — ``data/tag_wiki/verify_results_cpa.jsonl``
- glm (session model)     — ``data/tag_wiki/arbiter_glm/slices/out_*.jsonl``

Verdicts map to ``ok`` (accurate/minor) or ``bad`` (inaccurate); ``error``
means the judge itself failed for that entry. An entry needs verdicts from
all three parties; with only two parties present both must agree, otherwise
the entry lands in ``unresolved``. Quorum: >=2 bad votes = confirmed bad,
1 bad vote = disputed, 0 = ok.

Read-only. Writes ``arbiter_final.json`` / ``arbiter_final.txt`` next to
the results files.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

BAD = {"inaccurate"}
OK = {"accurate", "minor"}


def _load_jsonl(paths: list[Path]) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            key = f"{row.get('profile')}:{row.get('title')}"
            rows[key] = {
                "verdict": str(row.get("verdict", "error")),
                "reason": str(row.get("reason", "")),
            }
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gemma", default="data/tag_wiki/verify_results.jsonl")
    parser.add_argument("--cpa", default="data/tag_wiki/verify_results_cpa.jsonl")
    parser.add_argument("--glm-glob", default="data/tag_wiki/arbiter_glm/slices/out_*.jsonl")
    parser.add_argument("--out", default="data/tag_wiki/arbiter_final")
    args = parser.parse_args()

    gemma = _load_jsonl([Path(args.gemma)])
    cpa = _load_jsonl([Path(args.cpa)])
    glm = _load_jsonl(list(Path().glob(args.glm_glob)))

    # The re-judge scope is defined by gemma ∩ cpa (both sweep the whole scope);
    # glm may cover less when the content filter blocks explicit pages.
    scope = sorted(
        k
        for k in (set(gemma) & set(cpa))
        if gemma[k]["verdict"] in OK | BAD and cpa[k]["verdict"] in OK | BAD
    )
    unresolved: list[dict] = []
    confirmed_bad: list[dict] = []
    disputed: list[dict] = []
    ok = 0
    two_party_ok = 0

    for key in scope:
        votes = {
            "gemma": gemma[key],
            "cpa": cpa[key],
            "glm": glm.get(key),
        }
        usable = {
            party: v for party, v in votes.items() if v is not None and v["verdict"] in OK | BAD
        }
        bad_votes = sum(1 for v in usable.values() if v["verdict"] in BAD)
        entry: dict[str, Any] = {
            "key": key,
            "votes": {p: v["verdict"] for p, v in votes.items() if v is not None},
            "reasons": {
                p: v["reason"]
                for p, v in votes.items()
                if v is not None and v["verdict"] in BAD | {"error"}
            },
        }
        if len(usable) == 3:
            if bad_votes >= 2:
                confirmed_bad.append(entry)
            elif bad_votes == 1:
                disputed.append(entry)
            else:
                ok += 1
        elif len(usable) == 2:
            if bad_votes == 2:
                confirmed_bad.append(entry)
            elif bad_votes == 0:
                two_party_ok += 1
            else:
                entry["problem"] = "two-party-disagree"
                unresolved.append(entry)
        else:
            entry["problem"] = "single-judge-only"
            unresolved.append(entry)

    confirmed_bad.sort(
        key=lambda e: sum(1 for v in e["votes"].values() if v == "inaccurate"), reverse=True
    )

    by_profile: dict[str, dict[str, int]] = {}
    for entry in confirmed_bad:
        profile = entry["key"].split(":", 1)[0]
        by_profile.setdefault(profile, {})["confirmed_bad"] = (
            by_profile.setdefault(profile, {}).get("confirmed_bad", 0) + 1
        )
    for entry in disputed:
        profile = entry["key"].split(":", 1)[0]
        by_profile.setdefault(profile, {})["disputed"] = (
            by_profile.setdefault(profile, {}).get("disputed", 0) + 1
        )

    report = {
        "checked_scope": len(scope),
        "ok_three_party": ok,
        "ok_two_party_adopted": two_party_ok,
        "confirmed_bad": len(confirmed_bad),
        "disputed": len(disputed),
        "unresolved": len(unresolved),
        "party_coverage": {"gemma": len(gemma), "cpa": len(cpa), "glm": len(glm)},
        "party_verdicts": {
            "gemma": dict(Counter(v["verdict"] for v in gemma.values())),
            "cpa": dict(Counter(v["verdict"] for v in cpa.values())),
            "glm": dict(Counter(v["verdict"] for v in glm.values())),
        },
        "by_profile": by_profile,
        "confirmed_bad_entries": confirmed_bad[:100],
        "disputed_entries": disputed[:100],
        "unresolved_entries": unresolved[:100],
    }
    Path(args.out + ".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    lines = [
        "=== 三方复审裁定报告 ===",
        f"复审范围(gemma∩cpa): {len(scope)} 条 | GLM 全票通过: {ok} | 两方一致通过(GLM缺席): {two_party_ok}",
        f"确认有问题(>=2票 bad): {len(confirmed_bad)} | 分歧(1票 bad): {len(disputed)} | 未决: {len(unresolved)}",
        f"覆盖量 gemma={len(gemma)} cpa={len(cpa)} glm={len(glm)}",
        "",
        "--- 确认有问题 (前 50) ---",
    ]
    for entry in confirmed_bad[:50]:
        profile, title = entry["key"].split(":", 1)
        lines.append(f"- [{profile}] {title} | 票型 {entry['votes']}")
        for party, reason in entry["reasons"].items():
            if reason:
                lines.append(f"    {party}: {reason[:150]}")
    lines.append("")
    lines.append("--- 分歧 (前 30) ---")
    for entry in disputed[:30]:
        profile, title = entry["key"].split(":", 1)
        lines.append(f"- [{profile}] {title} | 票型 {entry['votes']}")
    if unresolved:
        lines.append("")
        lines.append(f"--- 未决 {len(unresolved)} 条 (单方报错或两方意见相左) ---")
        for entry in unresolved[:30]:
            profile, title = entry["key"].split(":", 1)
            lines.append(
                f"- [{profile}] {title} | 票型 {entry['votes']} | {entry.get('problem', '')}"
            )
    Path(args.out + ".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "checked_scope",
                    "ok_three_party",
                    "ok_two_party_adopted",
                    "confirmed_bad",
                    "disputed",
                    "unresolved",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
