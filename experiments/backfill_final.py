"""One-off correction of the final campaign's mechanism counts (run once, 2026-11-14).

    python -m experiments.backfill_final

The campaign counted the old database primary stepping down only through the FENCED,
DEPOSED and STARTUP_DEPOSED events. In X2/X8 the returning old primary is instead told to
step down by the monitor (POST /demote, event DEMOTED), so the column showed 0. This adds
`mechanisms.db_demoted` to every summary.json from that run's events.jsonl. The counter
(`endterm.mechanism_counts`) now records it directly, so new runs need no backfill.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "results" / "final"


def main() -> None:
    done = 0
    for summ in sorted(OUT.glob("X*/*/rep*/summary.json")):
        s = json.loads(summ.read_text())
        mech = s.get("mechanisms")
        events = summ.parent / "events.jsonl"
        if mech is None or "db_demoted" in mech or not events.exists():
            continue
        mech["db_demoted"] = sum(1 for line in events.open() if json.loads(line).get("ev") == "DEMOTED")
        summ.write_text(json.dumps(s, indent=1))
        done += 1
    print(f"added db_demoted to {done} summaries")


if __name__ == "__main__":
    main()
