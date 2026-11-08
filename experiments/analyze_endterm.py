"""Turn results/endterm/ into the tables and figures used in the report.

    python -m experiments.analyze_endterm

Writes:
  results/endterm/tables.md          markdown tables (fault suite, security, requirements)
  results/endterm/report_data.json   the numbers, for the report appendix
  results/endterm/fig_*.png          timelines, latency, detection-vs-recovery
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS = Path(__file__).resolve().parent.parent / "results" / "endterm"
BUILDS = ["baseline", "ft", "endterm"]
FAULTS = ["X1", "X2", "X3", "X4", "X7", "X8"]
SECURITY = ["X5", "X6"]


def load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def reps(exp: str, build: str) -> list[dict]:
    d = RESULTS / exp / build
    return [load(p) for p in sorted(d.glob("rep*/summary.json"))] if d.exists() else []


def ms(x):
    return "-" if x is None else f"{x:.0f}"


def mean_sd(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)


def cell(vals, fmt="{:.0f}"):
    m, sd = mean_sd(vals)
    if m is None:
        return "-"
    return fmt.format(m) if (sd or 0) < 1e-9 else f"{fmt.format(m)} ± {fmt.format(sd)}"


# ----------------------------------------------------------------- fault table

def fault_table() -> str:
    titles = {}
    rows = ["| Exp | Build | Reps | Failed reqs | Detect (alert) s | Detect (monitor) s | "
            "Recovery s | p95 ms | Consistent | PASS |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    data = {}
    for exp in FAULTS:
        for build in BUILDS:
            rs = reps(exp, build)
            if not rs:
                continue
            titles[exp] = rs[0]["title"]
            cons = sum(r["consistency"].get("consistent", False) for r in rs)
            passed = sum(bool(r.get("_pass")) for r in rs)
            rows.append("| {} | {} | {} | {} | {} | {} | {} | {} | {}/{} | {}/{} |".format(
                exp, build, len(rs),
                cell([r["failed"] for r in rs]),
                cell([r["detect_alert_s"] for r in rs], "{:.2f}"),
                cell([r["detect_event_s"] for r in rs], "{:.2f}"),
                cell([r["client_visible_recovery_s"] for r in rs], "{:.1f}"),
                cell([r["p95_ms"] for r in rs]),
                cons, len(rs), passed, len(rs)))
            data.setdefault(exp, {})[build] = {
                "reps": len(rs), "failed": mean_sd([r["failed"] for r in rs])[0],
                "detect_alert_s": mean_sd([r["detect_alert_s"] for r in rs])[0],
                "detect_event_s": mean_sd([r["detect_event_s"] for r in rs])[0],
                "recovery_s": mean_sd([r["client_visible_recovery_s"] for r in rs])[0],
                "p95_ms": mean_sd([r["p95_ms"] for r in rs])[0],
                "core_p95_ms": mean_sd([r.get("core_p95_ms") for r in rs])[0],
                "consistent_reps": cons, "passed": passed,
                "degraded": mean_sd([r["degraded"] for r in rs])[0],
                "recovered_by_retry": mean_sd([r["recovered_by_retry"] for r in rs])[0],
                "alerts": sorted({a for r in rs for a in r.get("alerts_fired", [])}),
                "consistency_example": rs[0]["consistency"],
            }
    out = ["## Fault-injection experiments (mean ± sd over repetitions)\n"]
    for exp in FAULTS:
        if exp in titles:
            out.append(f"**{exp} — {titles[exp]}**")
    return "\n".join(out) + "\n\n" + "\n".join(rows) + "\n", data


# ----------------------------------------------------------------- security table

def security_table() -> str:
    rows = ["| Exp | Build | Cases passed | Data unchanged | Audit chain | Audit records | Alerts | PASS |",
            "|---|---|---|---|---|---|---|---|"]
    data = {}
    for exp in SECURITY:
        s = load(RESULTS / exp / "endterm" / "summary.json")
        if not s:
            continue
        rows.append("| {} | endterm | {}/{} | {} | {} | {} | {} | {} |".format(
            exp, s["cases_passed"], s["cases_total"], "yes" if s["data_unchanged"] else "NO",
            "ok" if s["audit_chain_ok"] else "BROKEN", s["audit_records_for_denials"],
            ", ".join(s["alerts_fired"]) or "-", "PASS" if s["_pass"] else "FAIL"))
        data[exp] = {"cases_passed": s["cases_passed"], "cases_total": s["cases_total"],
                     "data_unchanged": s["data_unchanged"], "audit_chain_ok": s["audit_chain_ok"],
                     "cases": s["cases"], "extra": s.get("extra", {}), "passed": s["_pass"],
                     "alerts": s["alerts_fired"]}
    return "## Security experiments\n\n" + "\n".join(rows) + "\n", data


# ----------------------------------------------------------------- requirements

REQS = [
    ("R1", "≤1% requests fail on a single crash (X1)", "X1"),
    ("R2", "detection ≤1 s, interruption short (X1,X2)", "X2"),
    ("R4", "money consistent (X2,X3,X7)", "X7"),
    ("R5", "core p95 < 1 s under load (X4)", "X4"),
    ("R7", "unauthorized actions denied, data unchanged (X5)", "X5"),
    ("R8", "malformed/abuse handled, service stays up (X6)", "X6"),
    ("R9", "every incident detected by an alert (X2,X4)", "X2"),
    ("R10", "audit trail complete and tamper-evident (X5,X6)", "X5"),
    ("R12", "network partition: no split-brain, consistent (X8)", "X8"),
]


def requirements_table(fault, sec) -> str:
    def verdict(exp, build):
        if exp in fault and build in fault[exp]:
            d = fault[exp][build]
            return f"{d['passed']}/{d['reps']}"
        if exp in sec:
            return "PASS" if sec[exp]["passed"] else "FAIL"
        return "-"
    rows = ["| Req | Statement | baseline | ft | endterm |", "|---|---|---|---|---|"]
    for rid, text, exp in REQS:
        rows.append(f"| {rid} | {text} | {verdict(exp,'baseline')} | {verdict(exp,'ft')} | {verdict(exp,'endterm')} |")
    return "## Requirements verification (passed reps / total)\n\n" + "\n".join(rows) + "\n"


# ----------------------------------------------------------------- figures

def fig_timelines(data):
    exps = [e for e in FAULTS if e in data]
    n = len(exps)
    if not n:
        return
    cols = 3
    rowsn = (n + cols - 1) // cols
    fig, axes = plt.subplots(rowsn, cols, figsize=(13, 3.1 * rowsn), squeeze=False)
    for idx, exp in enumerate(exps):
        ax = axes[idx // cols][idx % cols]
        for build, color in [("baseline", "#c0392b"), ("ft", "#2980b9"), ("endterm", "#27ae60")]:
            d = RESULTS / exp / build / "rep1"
            csv = d / "requests.csv"
            if not csv.exists():
                continue
            import csv as csvmod
            buckets: dict[int, list[int]] = {}
            for row in csvmod.DictReader(open(csv)):
                t = float(row["t0"])
                ok = row["ok"] == "True"
                buckets.setdefault(int(t), []).append(1 if ok else 0)
            xs = sorted(buckets)
            ys = [100 * sum(buckets[x]) / len(buckets[x]) for x in xs]
            ax.plot(xs, ys, color=color, label=build, linewidth=1.6)
        ax.axvspan(10, 25, color="0.85", zorder=0)
        ax.set_title(f"{exp}", fontsize=10)
        ax.set_ylim(-3, 103)
        ax.set_xlabel("time (s)", fontsize=8)
        ax.set_ylabel("% ok", fontsize=8)
        ax.tick_params(labelsize=7)
    for j in range(n, rowsn * cols):
        axes[j // cols][j % cols].axis("off")
    axes[0][0].legend(fontsize=8, loc="lower right")
    fig.suptitle("Successful requests per second (grey = fault active), rep 1", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(RESULTS / "fig_timelines.png", dpi=130)
    plt.close(fig)


def fig_detect_recover(data):
    exps = [e for e in FAULTS if e in data and "endterm" in data[e]]
    if not exps:
        return
    fig, ax = plt.subplots(figsize=(9, 4.5))
    import numpy as np
    x = np.arange(len(exps))
    det = [data[e]["endterm"].get("detect_event_s") or 0 for e in exps]
    rec = [data[e]["endterm"].get("recovery_s") or 0 for e in exps]
    alert = [data[e]["endterm"].get("detect_alert_s") or 0 for e in exps]
    ax.bar(x - 0.25, det, 0.25, label="detected (monitor/event)", color="#2980b9")
    ax.bar(x, alert, 0.25, label="detected (alert fired)", color="#8e44ad")
    ax.bar(x + 0.25, rec, 0.25, label="client-visible recovery", color="#27ae60")
    ax.set_xticks(x)
    ax.set_xticklabels(exps)
    ax.set_ylabel("seconds after injection")
    ax.set_title("Endterm build: detection vs. recovery per experiment")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(RESULTS / "fig_detect_recover.png", dpi=130)
    plt.close(fig)


def main():
    ft_md, fault = fault_table()
    sec_md, sec = security_table()
    req_md = requirements_table(fault, sec)
    (RESULTS / "tables.md").write_text("\n\n".join([ft_md, sec_md, req_md]))
    (RESULTS / "report_data.json").write_text(json.dumps({"faults": fault, "security": sec}, indent=1, default=str))
    fig_timelines(fault)
    fig_detect_recover(fault)
    print("wrote tables.md, report_data.json, fig_timelines.png, fig_detect_recover.png")
    print("\n" + ft_md + "\n" + sec_md + "\n" + req_md)


if __name__ == "__main__":
    main()
