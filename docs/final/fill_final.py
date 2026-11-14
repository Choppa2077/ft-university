"""Build docs/final/Final_Report.md from the template Final_Report.src.md and the measured data
(run `python -m experiments.analyze_final` first). Nothing is typed in by hand: every number in
the generated parts comes from results/final/report_data.json or results/final/tables.md.

    python docs/final/fill_final.py && bash docs/final/to_docx.sh

It also refreshes the results section of the root README (between <!--README_RESULTS--> markers).

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
    "CONFIG": "Configuration difference between the builds",
    "DOUBLEFAULT": "Double fault in the soak (event log excerpt)",
}


README = ROOT / "README.md"
BUILDS = ["baseline", "ft", "endterm"]
README_ROWS = [("X0", "none (steady state)"), ("X1", "service instance crash"), ("X2", "database primary frozen"),
               ("X3", "slow bank"), ("X4", "5× load spike"), ("X7", "interrupted payments"),
               ("X8", "database primary partitioned"), ("X9", "gray failure")]


def readme_block(data: dict) -> str:
    """The results section of the root README, from the same data as the report."""
    f, d, rel, sec = data["faults"], data["derived"], data["reliability"], data["security"]
    lines = ["## Results of the final campaign", "",
             f"{d['total_runs']} repetitions, each on a fresh cluster ({d['harness_retries']} harness retries): "
             "8 fault experiments × 3 builds × 5 repetitions and 2 security experiments × 3, plus a 900 s crash "
             "soak per build. Means per run; full tables in `results/final/tables.md`, discussion in the final report.",
             "", "| Exp | Fault | Failed requests: baseline / ft / endterm | Orphan debits / double charges: "
             "baseline → endterm | PASS: baseline · ft · endterm |", "|---|---|---|---|---|"]
    for exp, name in README_ROWS:
        s = f[exp]
        failed = " / ".join(f"{s[b]['failed']['mean']:.0f}" for b in BUILDS)
        money = (f"{s['baseline']['orphan_debits']['mean']:.0f} / {s['baseline']['double_charged']['mean']:.0f} → "
                 f"{s['endterm']['orphan_debits']['mean']:.0f} / {s['endterm']['double_charged']['mean']:.0f}")
        passed = " · ".join(f"{s[b]['passed']}/{s[b]['reps']}" for b in BUILDS)
        lines.append(f"| {exp} | {name} | {failed} | {money} | {passed} |")
    b, e = rel["baseline"], rel["endterm"]
    slos = data["slos"]["endterm"]
    n_slos = len({k.rstrip("ab") for k in slos})                     # SLO-4 has parts a and b
    verdict = (f"endterm meets all {n_slos}" if all(v["met"] for v in slos.values())
               else "endterm misses " + ", ".join(k for k, v in slos.items() if v["met"] is False))
    missed = ", ".join(k for k, v in data["slos"]["baseline"].items() if v["met"] is False)
    lines += ["",
              f"- Security (endterm): every probe refused in every run — X5 {sec['X5']['cases']}, X6 "
              f"{sec['X6']['cases']}; data unchanged and the audit chain verified after each run.",
              f"- Soak, 900 s with random crashes: system availability "
              f"{b['services']['system']['availability_time']:.4f} in the baseline ({b['kills_done']} crashes) and "
              f"{e['services']['system']['availability_time']:.4f} in endterm ({e['kills_done']} crashes); "
              f"block-diagram prediction {b['predicted']['system']:.4f} and {e['predicted']['system']:.4f}.",
              f"- Service-level objectives: {verdict}; the baseline misses {missed}.",
              "- X9 passes when telemetry names the faulty replica, which works in every build; the failed "
              "requests show that only the replicated builds also limit the damage.",
              f"- Cost (X0): {f['X0']['endterm']['app_containers']} instead of {f['X0']['baseline']['app_containers']} "
              f"containers, +{d['x0']['rss_increase_pct']} % memory, +{d['x0']['cpu_increase_pct']} % CPU, "
              f"+{d['x0']['pay_p50_delta_ms']} ms median payment latency; no alert in a fault-free run."]
    return "\n".join(lines)


def fill_readme(data: dict) -> None:
    start, end = "<!--README_RESULTS-->", "<!--/README_RESULTS-->"
    text = README.read_text()
    if start not in text:
        return
    block = f"{start}\n{readme_block(data)}\n{end}"
    if end in text:
        text = text[:text.index(start)] + block + text[text.index(end) + len(end):]
    else:
        text = text.replace(start, block)
    README.write_text(text)
    print(f"updated {README.relative_to(ROOT)} (results section)")


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
    fill_readme(data)
    if left:
        print("WARNING: unresolved placeholders:", left)


if __name__ == "__main__":
    main()
