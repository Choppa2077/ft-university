"""Build docs/final/Final_Report.md from the template Final_Report.src.md and the measured data
(run `python -m experiments.analyze_final` first). Nothing is typed in by hand: every number in
the generated parts comes from results/final/report_data.json or results/final/tables.md.

    python docs/final/fill_final.py && bash docs/final/to_docx.sh

It also refreshes the results section of the root README (between <!--README_RESULTS--> markers).

Placeholders in the template:
  <!--TABLE:NAME-->           a table section of results/final/tables.md (see TABLES below);
                              MECH_COMPACT and ALERTS_COMPACT condense the full matrices to one
                              row per experiment (see COMPACT)
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


# The three widest tables of tables.md (15, 15 and 11 columns) do not fit a portrait page, so the
# report shows each as two narrower tables over the same rows; tables.md keeps them whole.
SPLITS = {
    "RESULTS": [
        ("Availability and latency", ["Exp", "Scenario", "Build", "Reps", "Success %", "Failed req. after inj.",
                                      "Failed user actions", "Recovery s", "p95 ms", "Core p95 ms"]),
        ("Detection and integrity", ["Exp", "Build", "Detect (event) s", "First related alert s",
                                     "Orphan debits / double charges per run", "Consistent", "PASS"]),
    ],
    "MECH": [
        ("Replicas, database and load", ["Exp", "Build", "GW retries", "replica marked down", "restarts",
                                         "DB failovers", "old primary stepped down", "shed", "stale answers"]),
        ("Payments", ["Exp", "Build", "breaker opened", "bank retries", "queued (202)", "roll fwd", "roll back",
                      "duplicates caught"]),
    ],
    "TRADEOFFS": [
        ("Latency (ms)", ["Build", "p50 ms", "p95 ms", "p99 ms", "Payment p50 / p95 ms",
                          "Registration p50 / p95 ms", "Core p95 ms (histogram)"]),
        ("Resources and noise", ["Build", "App containers", "CPU (cores, avg)", "RSS (MB)",
                                 "Alerts in a fault-free run"]),
    ],
}


def split_table(md: str, groups: list[tuple[str, list[str]]]) -> str:
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in md.splitlines()
            if line.startswith("|")]
    header, body = rows[0], rows[2:]
    parts = []
    for title, cols in groups:
        idx = [header.index(c) for c in cols]
        lines = ["| " + " | ".join(header[i] for i in idx) + " |", "|" + "---|" * len(idx)]
        lines += ["| " + " | ".join(r[i] for i in idx) + " |" for r in body]
        parts.append(f"*{title}*\n\n" + "\n".join(lines))
    if sorted({i for _, cols in groups for i in (header.index(c) for c in cols)}) != list(range(len(header))):
        raise SystemExit("split leaves out a column")
    return "\n\n".join(parts)


def md_rows(md: str) -> tuple[list[str], list[list[str]]]:
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in md.splitlines()
            if line.startswith("|")]
    return rows[0], rows[2:]


def _mean(cell: str) -> float:
    return float(cell.split("±")[0])


def _num(x: float) -> str:
    return f"{x:.0f}" if x >= 100 or x == round(x) else f"{x:.1f}"


# The full mechanism and alert matrices (24 rows each, mostly zeros) stay in results/final/tables.md;
# the report shows one row per experiment with only what actually happened.
def compact_mech(md: str, scenario: dict[str, str]) -> str:
    header, body = md_rows(md)
    cols = header[2:]
    by = {(r[0], r[1]): dict(zip(cols, r[2:])) for r in body}
    lines = ["| Exp | Scenario | Mechanisms that acted, mean per run: ft / endterm | Baseline |", "|---|---|---|---|"]
    for exp in dict.fromkeys(r[0] for r in body):
        ft, et, base = by[(exp, "ft")], by[(exp, "endterm")], by[(exp, "baseline")]
        acted = []
        for c in cols:
            if _mean(ft[c]) or _mean(et[c]):
                a, b = _num(_mean(ft[c])), _num(_mean(et[c]))
                acted.append(f"{c} {a}" if a == b else f"{c} {a} / {b}")
        base_acted = [f"{c} {_num(_mean(base[c]))}" for c in cols if _mean(base[c])]
        lines.append(f"| {exp} | {scenario[exp]} | {', '.join(acted) or 'none'} | {', '.join(base_acted) or 'none'} |")
    return "\n".join(lines)


def compact_alerts(md: str, scenario: dict[str, str]) -> str:
    header, body = md_rows(md)
    col = {name: i for i, name in enumerate(header)}
    by = {(r[0], r[1]): r for r in body}
    lines = ["| Exp | Scenario | Runs with a related alert: baseline · ft · endterm | First related alert, s: "
             "baseline / ft / endterm | Collateral / false positives |", "|---|---|---|---|---|"]
    for exp in dict.fromkeys(r[0] for r in body):
        rs = [by[(exp, b)] for b in BUILDS]
        runs = " · ".join(r[col["Runs detected by a related alert"]] if r[col["Alert required"]] == "yes" else "–"
                          for r in rs)
        first = " / ".join(f"{_mean(r[col['First related alert s']]):.1f}"
                           if r[col["First related alert s"]] != "–" else "–" for r in rs)
        coll = [(b, r[col["Collateral alerts (symptom confirmed)"]]) for b, r in zip(BUILDS, rs)]
        n_coll = sum(int(v.split()[0]) for _, v in coll)
        n_fp = sum(int(r[col["False positives"]]) for r in rs)
        names = "; ".join(f"{b}: {v.split(' ', 1)[1].strip('()')}" for b, v in coll if v.split()[0] != "0")
        lines.append(f"| {exp} | {scenario[exp]} | {runs} | {first} | {n_coll} / {n_fp}"
                     + (f" ({names})" if names else "") + " |")
    return "\n".join(lines)


COMPACT = {"MECH_COMPACT": ("MECH", compact_mech), "ALERTS_COMPACT": ("ALERTS", compact_alerts)}


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
        body = section(tables, title)
        text = text.replace(f"<!--TABLE:{key}-->", split_table(body, SPLITS[key]) if key in SPLITS else body)
    _, res = md_rows(section(tables, TABLES["RESULTS"]))
    scenario = {r[0]: r[1] for r in res}
    for key, (src, build) in COMPACT.items():
        text = text.replace(f"<!--TABLE:{key}-->", build(section(tables, TABLES[src]), scenario))

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
