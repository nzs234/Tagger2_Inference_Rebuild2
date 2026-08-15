"""Build the immutable e621 replacement index with ``pass`` rows dropped.

The source index keeps an explicit ``pass`` action for identity passthrough.
The production e621 profile requested by Tagger2 uses the same rows as a
drop-list instead.  This tool is deliberately narrow: it only transforms
``pass`` rows and preserves all other CSV fields and row order byte-for-byte
at the CSV value level.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from tagger2.workflow.replacement_index import REPLACEMENT_CSV_HEADER


def convert(source: Path, destination: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8-sig", newline="") as input_stream:
        reader = csv.DictReader(input_stream)
        if tuple(reader.fieldnames or ()) != REPLACEMENT_CSV_HEADER:
            raise ValueError(
                "replacement index header must be "
                + ",".join(REPLACEMENT_CSV_HEADER)
            )
        with destination.open("w", encoding="utf-8", newline="\n") as output_stream:
            writer = csv.DictWriter(
                output_stream,
                fieldnames=list(REPLACEMENT_CSV_HEADER),
                lineterminator="\n",
            )
            writer.writeheader()
            for row in reader:
                action = str(row["action"]).strip()
                if action == "pass":
                    action = "drop"
                    row["replacement_tags"] = ""
                row["action"] = action
                writer.writerow({field: row[field] for field in REPLACEMENT_CSV_HEADER})
                counts[action] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    counts = convert(args.source, args.destination)
    print("Converted replacement index:")
    for action in ("keep", "replace", "drop", "pass"):
        print(f"  {action}: {counts[action]}")
    if counts["pass"]:
        raise SystemExit("conversion left pass rows behind")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
