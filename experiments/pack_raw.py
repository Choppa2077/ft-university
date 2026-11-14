"""Pack the raw per-run logs of the final campaign for the repository.

    python -m experiments.pack_raw

The raw files are large and git-ignored where they are written; this puts compressed copies in
results/final/raw/:
  requests_all.tar.gz   every client request of every repetition (requests.csv) and of the soak
  events_rep1.tar.gz    every structured event of repetition 1 of each experiment and build,
                        and of the soak runs (events.jsonl)
Per-run summaries (summary.json) and aggregates stay uncompressed next to them.
"""
from __future__ import annotations

import tarfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "results" / "final"


def pack(name: str, files: list[Path]) -> None:
    dest = OUT / "raw" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz", compresslevel=9) as tar:
        for f in sorted(files):
            tar.add(f, arcname=str(f.relative_to(OUT)))
    print(f"{dest.relative_to(OUT.parent.parent)}: {len(files)} files, {dest.stat().st_size / 2 ** 20:.1f} MB")


def main() -> None:
    requests = [p for p in OUT.rglob("requests.csv") if "raw" not in p.parts]
    events = [p for p in OUT.rglob("events.jsonl")
              if "raw" not in p.parts and (p.parent.name == "rep1" or "soak" in p.parts)]
    pack("requests_all.tar.gz", requests)
    pack("events_rep1.tar.gz", events)


if __name__ == "__main__":
    main()
