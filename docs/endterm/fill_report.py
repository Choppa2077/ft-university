"""Substitute the <!--V:...--> and <!--TABLE:...--> placeholders in Endterm_Report.md with
the real numbers from results/endterm/ (run analyze_endterm.py first).

    python docs/endterm/fill_report.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = ROOT / "results" / "endterm"
REPORT = ROOT / "docs" / "endterm" / "Endterm_Report.md"

data = json.loads((RESULTS / "report_data.json").read_text())
faults, sec = data["faults"], data["security"]
tables = (RESULTS / "tables.md").read_text()


def num(x, nd=0):
    if x is None:
        return "—"
    return f"{x:.{nd}f}"


def fval(exp, build, key, nd=0):
    try:
        return num(faults[exp][build][key], nd)
    except (KeyError, TypeError):
        return "—"


# scalar placeholders
V = {
    "X1.baseline.failed": fval("X1", "baseline", "failed"),
    "X1.baseline.recovery": fval("X1", "baseline", "recovery_s", 1),
    "X1.endterm.detect_event": fval("X1", "endterm", "detect_event_s", 2),
    "X2.baseline.failed": fval("X2", "baseline", "failed"),
    "X4.endterm.core_p95": fval("X4", "endterm", "core_p95_ms"),
    "X5.cases": f"{sec.get('X5', {}).get('cases_passed', '?')}/{sec.get('X5', {}).get('cases_total', '?')}",
    "X6.cases": f"{sec.get('X6', {}).get('cases_passed', '?')}/{sec.get('X6', {}).get('cases_total', '?')}",
}


def compare(exp):
    b, e = faults.get(exp, {}).get("baseline", {}), faults.get(exp, {}).get("endterm", {})
    bc = b.get("consistent_reps", "?")
    ec = e.get("consistent_reps", "?")
    br = faults.get(exp, {}).get("baseline", {}).get("reps", "?")
    er = faults.get(exp, {}).get("endterm", {}).get("reps", "?")
    return (f"baseline failed {num(b.get('failed'))}, consistent {bc}/{br}; "
            f"endterm failed {num(e.get('failed'))}, consistent {ec}/{er}")


V["X3.compare"] = compare("X3")
V["X7.compare"] = compare("X7")
V["X8.endterm"] = (f"endterm failed {fval('X8','endterm','failed')}, consistent "
                   f"{faults.get('X8',{}).get('endterm',{}).get('consistent_reps','?')}/"
                   f"{faults.get('X8',{}).get('endterm',{}).get('reps','?')}")
flood = sec.get("X6", {}).get("extra", {}).get("abuse_flood", {})
V["X6.flood"] = (f"{flood.get('429','?')} of {flood.get('sent','?')} requests throttled with 429"
                 if flood else "throttled")

# table sections (split tables.md on its headings)
def section(title):
    m = re.search(rf"## {re.escape(title)}.*?(?=\n## |\Z)", tables, re.S)
    return m.group(0).strip() if m else f"*(table {title} not found)*"


TABLES = {
    "FAULTS": section("Fault-injection experiments (mean ± sd over repetitions)"),
    "SECURITY": section("Security experiments"),
    "REQUIREMENTS": section("Requirements verification (repetitions meeting the requirement / total)"),
}

text = REPORT.read_text()
for key, val in V.items():
    text = text.replace(f"<!--V:{key}-->", val)
for key, val in TABLES.items():
    text = text.replace(f"<!--TABLE:{key}-->", val)

left = re.findall(r"<!--(V|TABLE):[^>]+-->", text)
REPORT.write_text(text)
print(f"filled {len(V)} values and {len(TABLES)} tables into {REPORT.name}")
if left:
    print("WARNING: unresolved placeholders:", left)
