"""Build docs/final/Final_Report.md from the template Final_Report.src.md and the measured data
(run `python -m experiments.analyze_final` first). Nothing is typed in by hand: every number in
the generated parts comes from results/final/report_data.json or results/final/tables.md.

    python docs/final/fill_final.py && bash docs/final/to_docx.sh

Placeholders in the template:
  <!--TABLE:NAME-->           a table section of results/final/tables.md (see TABLES below)
  <!--V:dotted.path|fmt-->    one value from report_data.json, e.g.
                              <!--V:faults.X2.baseline.failed.mean|.0f-->
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "docs" / "final" / "Final_Report.src.md"
DST = ROOT / "docs" / "final" / "Final_Report.md"
RESULTS = ROOT / "results" / "final"

TABLES = {
    "RESULTS": "Consolidated results (fault experiments)",
    "MECH": "Mechanism activations (mean per run)",
    "ALERTS": "Alert correctness",
    "TRADEOFFS": "Trade-offs in a fault-free run (X0)",
    "SECURITY": "Security experiments",
    "RELIABILITY": "Reliability (soak test)",
    "SLOS": "SLO verdicts",
}


def section(tables: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", tables, re.S | re.M)
    if not m:
        raise SystemExit(f"table section not found: {title}")
    return m.group(1).strip()


def value(data: dict, path: str, fmt: str) -> str:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    if cur is None:
        return "–"
    return format(cur, fmt) if fmt else str(cur)


def main() -> None:
    data = json.loads((RESULTS / "report_data.json").read_text())
    tables = (RESULTS / "tables.md").read_text()
    text = SRC.read_text()
    for key, title in TABLES.items():
        text = text.replace(f"<!--TABLE:{key}-->", section(tables, title))

    def sub(m: re.Match) -> str:
        path, _, fmt = m.group(1).partition("|")
        return value(data, path, fmt)

    text = re.sub(r"<!--V:([^>]+?)-->", sub, text)
    left = re.findall(r"<!--(?:V|TABLE|ABSTRACT|SECTION4_RESULTS|RESULTS_SECTIONS|APPENDIX)[^>]*-->", text)
    DST.write_text(text)
    print(f"wrote {DST.relative_to(ROOT)}")
    if left:
        print("WARNING: unresolved placeholders:", left)


if __name__ == "__main__":
    main()
