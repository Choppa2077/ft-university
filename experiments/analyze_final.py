"""Analysis of the final-project campaign (results/final): consolidated results table,
alert correctness, mechanism activations, trade-offs, reliability from the soak test with
the reliability-block-diagram prediction, SLO and requirement verdicts, figures.

    python -m experiments.analyze_final      # -> results/final/tables.md, report_data.json, fig_*.png

Reliability metrics use the same method as the midterm (experiments/analyze.py):
component downtime from the monitor's health checks, user-visible outages from 1 s windows
of the client log, a payment and its retry counting as one user action.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from ftuni import config

from .analyze import KINDS, component_metrics, per_second, system_metrics, user_level
from .workload import load_csv

OUT = Path(__file__).resolve().parent.parent / "results" / "final"
BUILDS = ["baseline", "ft", "endterm"]
FAULT_EXPS = ["X0", "X1", "X2", "X3", "X4", "X7", "X8", "X9"]
SEC_EXPS = ["X5", "X6"]
TITLES = {
    "X0": "steady state, no fault", "X1": "service instance crash", "X2": "database primary frozen 12 s",
    "X3": "bank 6 s slow for 15 s", "X4": "load spike +180 timetable req/s", "X5": "unauthorized access",
    "X6": "malformed / abusive requests", "X7": "30% payments interrupted after debit",
    "X8": "primary partitioned 12 s", "X9": "gray failure (replica 50% HTTP 500)",
}
FAULT_LEN = {"X1": 0.0, "X2": 12.0, "X3": 15.0, "X4": 15.0, "X7": 15.0, "X8": 12.0}
COLORS = {"baseline": "#c0392b", "ft": "#2a78d6", "endterm": "#1e9e5a"}


# ----------------------------------------------------------------- loading and stats

def runs(exp: str, build: str) -> list[dict]:
    out = []
    for p in sorted((OUT / exp / build).glob("rep*/summary.json"), key=lambda p: int(p.parent.name[3:])):
        s = json.loads(p.read_text())
        s["_dir"] = str(p.parent)
        out.append(s)
    return out


def stat(vals) -> dict | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 4), "sd": round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0,
            "min": min(vals), "max": max(vals), "n": len(vals)}


def fmt(st: dict | None, nd: int = 1) -> str:
    if st is None:
        return "–"
    if st["n"] == 1 or st["sd"] == 0:
        return f"{st['mean']:.{nd}f}"
    return f"{st['mean']:.{nd}f} ± {st['sd']:.{nd}f}"


def user_failed_after(s: dict) -> tuple[int, int] | None:
    """User actions (a payment and its retry = one action) that failed after the injection."""
    csv = Path(s["_dir"]) / "requests.csv"
    if not csv.exists():
        return None
    recs = [r for r in load_csv(csv) if r.t1 > 0 and r.t0 >= s.get("t_inject_rel", 10.0) - 0.5]
    total, failed = user_level(recs)
    return failed, total


# ----------------------------------------------------------------- alert correctness

def related(exp: str, a: dict) -> bool:
    """Is this alert about the injected fault or a direct consequence of it (TEST_PLAN, section 5)?"""
    n, lab = a["alertname"], a.get("labels") or {}
    name, svc = str(lab.get("name", "")), str(lab.get("service", ""))
    db_users = ("student", "payment", "records")
    if exp == "X1":
        return ((n == "InstanceDown" and name == "payment-a") or (n == "ReplicaErrorRate" and name.startswith("payment"))
                or (n in ("UserFacingErrorRate", "HighLatencyP95") and svc == "payment"))
    if exp in ("X2", "X8"):
        return ((n == "InstanceDown" and name.startswith("db-")) or n in ("DatabaseFailover", "DBReplicationLost")
                or (n in ("UserFacingErrorRate", "HighLatencyP95") and svc in db_users)
                or (n == "ReplicaErrorRate" and not name.startswith("timetable")))
    if exp == "X3":
        return ((n == "InstanceDown" and name == "bank") or n in ("CircuitBreakerOpen", "PaymentsUnfinished")
                or (n in ("UserFacingErrorRate", "HighLatencyP95") and svc == "payment")
                or (n == "ReplicaErrorRate" and name.startswith("payment")))
    if exp == "X4":
        return n in ("LoadShedding", "HighLatencyP95", "UserFacingErrorRate", "ReplicaErrorRate", "InstanceDown")
    if exp == "X7":
        return (n == "PaymentsUnfinished" or (n in ("UserFacingErrorRate", "HighLatencyP95") and svc == "payment")
                or (n == "ReplicaErrorRate" and name.startswith("payment")))
    if exp == "X9":
        return ((n == "ReplicaErrorRate" and name == "student-a") or (n == "InstanceDown" and name == "student-a")
                or (n in ("UserFacingErrorRate", "HighLatencyP95") and svc == "student"))
    return False      # X0: nothing is wrong, every alert is a false positive


def required(exp: str, build: str) -> bool:
    """Must a related alert fire? Only incidents that outlast the 10 s alert window (R9)."""
    if exp == "X0":
        return False
    if exp == "X1":
        return build == "baseline"       # masked within ~1 s in the replicated builds
    return True


def alert_eval(exp: str, build: str, s: dict) -> dict:
    firing, seen = [], set()
    for a in s.get("alerts_detail", []):
        if a.get("status") != "firing":
            continue
        key = (a["alertname"], tuple(sorted((a.get("labels") or {}).items())))
        if key in seen:
            continue
        seen.add(key)
        firing.append(a)
    rel = [a for a in firing if related(exp, a) and a["t_rel_inject"] >= -0.2]
    fp = [a for a in firing if not related(exp, a)]
    wrong = [a for a in firing if exp == "X9" and a["alertname"] == "ReplicaErrorRate"
             and (a.get("labels") or {}).get("name") != "student-a"]
    return {"required": required(exp, build), "detected": bool(rel),
            "first_related_s": min((a["t_rel_inject"] for a in rel), default=None),
            "false_positives": len(fp), "fp_names": sorted({a["alertname"] for a in fp}),
            "wrong_diagnosis": len(wrong), "alerts": len(firing)}


# ----------------------------------------------------------------- per experiment and build

MECH_KEYS = ["gateway_retries", "upstream_marked_down", "breaker_opened", "bank_retries", "payments_queued",
             "recovered_roll_forward", "recovered_roll_back", "compensations", "duplicate_requests",
             "db_failovers", "db_client_switches", "db_fenced_or_deposed", "db_resyncs", "gateway_shed",
             "gateway_stale_answers", "gateway_fast_fail", "container_restarts"]


def summarize(exp: str, build: str) -> dict | None:
    rs = runs(exp, build)
    if not rs:
        return None
    ae = [alert_eval(exp, build, s) for s in rs]
    uf = [user_failed_after(s) for s in rs]
    out = {
        "reps": len(rs), "passed": sum(bool(s.get("_pass")) for s in rs),
        "consistent": sum(bool(s["consistency"].get("consistent")) for s in rs),
        "requests": stat([s["requests"] for s in rs]),
        "success_rate": stat([s["success_rate"] for s in rs]),
        "success_pct": stat([100 * s["success_rate"] for s in rs]),
        "failed": stat([s["failed"] for s in rs]),
        "failed_ratio": stat([s["failed"] / max(1, s["requests"]) for s in rs]),
        "user_failed": stat([u[0] for u in uf if u]),
        "degraded": stat([s["degraded"] for s in rs]),
        "recovered_by_retry": stat([s["recovered_by_retry"] for s in rs]),
        "p50_ms": stat([s["p50_ms"] for s in rs]), "p95_ms": stat([s["p95_ms"] for s in rs]),
        "p99_ms": stat([s["p99_ms"] for s in rs]), "core_p95_ms": stat([s.get("core_p95_ms") for s in rs]),
        "detect_event_s": stat([s.get("detect_event_s") for s in rs]),
        "detect_alert_s": stat([s.get("detect_alert_s") for s in rs]),
        "first_related_alert_s": stat([a["first_related_s"] for a in ae]),
        "recovery_s": stat([s["client_visible_recovery_s"] for s in rs]),
        "alerts_required": ae[0]["required"],
        "alerts_detected": sum(a["detected"] for a in ae),
        "false_positive_alerts": sum(a["false_positives"] for a in ae),
        "fp_names": sorted({n for a in ae for n in a["fp_names"]}),
        "wrong_diagnosis": sum(a["wrong_diagnosis"] for a in ae),
        "alerts_fired": sorted({n for s in rs for n in s.get("alerts_fired", [])}),
        "mechanisms": {k: stat([(s.get("mechanisms") or {}).get(k) for s in rs]) for k in MECH_KEYS},
        "cpu_cores": stat([(s.get("resources") or {}).get("cpu_cores") for s in rs]),
        "rss_mb": stat([(s.get("resources") or {}).get("rss_mb") for s in rs]),
        "app_containers": (rs[0].get("resources") or {}).get("app_containers"),
        "orphan_debits": stat([s["consistency"].get("orphan_debits") for s in rs]),
        "double_charged": stat([s["consistency"].get("double_charged_intents") for s in rs]),
        "replica_identical": sum(bool((s["consistency"].get("replica") or {}).get("identical")) for s in rs),
    }
    if exp == "X9":
        inj = [s.get("inject", {}) for s in rs]
        out["diagnosed_correctly"] = sum(i.get("diagnosed") == "student-a" for i in inj)
        out["diagnosed_after_s"] = stat([i.get("diagnosed_after_s") for i in inj])
        out["mitigated_after_s"] = stat([i.get("mitigated_after_s") for i in inj])
    return out


def security(exp: str) -> dict | None:
    rs = [json.loads(p.read_text()) for p in sorted((OUT / exp / "endterm").glob("rep*/summary.json"))]
    if not rs:
        return None
    return {"reps": len(rs), "passed": sum(bool(s["_pass"]) for s in rs),
            "cases": f"{rs[0]['cases_passed']}/{rs[0]['cases_total']}",
            "cases_all_reps": all(s["cases_passed"] == s["cases_total"] for s in rs),
            "data_unchanged": sum(bool(s["data_unchanged"]) for s in rs),
            "audit_ok": sum(bool(s["audit_chain_ok"]) for s in rs),
            "audit_records": stat([s["audit_records_for_denials"] for s in rs]),
            "alerts": sorted({n for s in rs for n in s.get("alerts_fired", [])}),
            "flood": (rs[0].get("extra") or {}).get("abuse_flood")}


# ----------------------------------------------------------------- reliability (soak)

def rbd(kinds: dict, mode: str) -> dict:
    """Reliability block diagram from the measured component availabilities: the five kinds
    in series; in a replicated build each kind is a parallel pair (instant, perfect failover)."""
    a = {k: v["availability"] for k, v in kinds.items()}
    rep = mode in config.REPLICATED
    blocks = {k: (1 - (1 - a[k]) ** 2) if rep else a[k] for k in KINDS}
    sys_a = 1.0
    for v in blocks.values():
        sys_a *= v
    return {"blocks": {k: round(v, 6) for k, v in blocks.items()}, "system": round(sys_a, 6)}


def soak(mode: str) -> dict | None:
    d = OUT / "soak" / mode
    if not (d / "meta.json").exists():
        return None
    meta = json.loads((d / "meta.json").read_text())
    events = [json.loads(line) for line in (d / "events.jsonl").read_text().splitlines() if line.strip()]
    recs = load_csv(d / "requests.csv")
    comp = component_metrics(events, meta["origin"], meta["duration"], mode)
    sysm = system_metrics(recs, meta["duration"])
    s = sysm["system"]
    up_h = (meta["duration"] - s["downtime_s"]) / 3600
    return {"duration_s": meta["duration"], "rate": meta["rate"], "mttf_injected_s": meta["mttf_injected_s"],
            "kills_planned": meta["kills_planned"], "kills_done": meta["kills_done"],
            "components": comp["kinds"], "instances": comp["instances"], "services": sysm,
            "system_failure_rate_per_h": round(s["outages"] / up_h, 2) if up_h > 0 else None,
            "predicted": rbd(comp["kinds"], mode)}


# ----------------------------------------------------------------- tables

def table_results(data: dict) -> list[str]:
    rows = ["| Exp | Scenario | Build | Reps | Success % | Failed req. after inj. | Failed user actions | "
            "Detect (event) s | First related alert s | Recovery s | p95 ms | Core p95 ms | Consistent | PASS |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for exp in FAULT_EXPS:
        for b in BUILDS:
            s = data["faults"].get(exp, {}).get(b)
            if not s:
                continue
            rows.append(f"| {exp} | {TITLES[exp]} | {b} | {s['reps']} | {fmt(s['success_pct'], 2)} | "
                        f"{fmt(s['failed'], 0)} | {fmt(s['user_failed'], 0)} | {fmt(s['detect_event_s'], 2)} | "
                        f"{fmt(s['first_related_alert_s'], 2)} | {fmt(s['recovery_s'], 1)} | {fmt(s['p95_ms'], 0)} | "
                        f"{fmt(s['core_p95_ms'], 0)} | {s['consistent']}/{s['reps']} | {s['passed']}/{s['reps']} |")
    return rows


def table_mechanisms(data: dict) -> list[str]:
    cols = [("gateway_retries", "GW retries"), ("upstream_marked_down", "replica marked down"),
            ("breaker_opened", "breaker opened"), ("bank_retries", "bank retries"), ("payments_queued", "queued (202)"),
            ("recovered_roll_forward", "roll fwd"), ("recovered_roll_back", "roll back"),
            ("duplicate_requests", "duplicates caught"), ("db_failovers", "DB failovers"),
            ("db_fenced_or_deposed", "fenced"), ("gateway_shed", "shed"), ("gateway_stale_answers", "stale answers"),
            ("container_restarts", "restarts")]
    rows = ["| Exp | Build | " + " | ".join(c[1] for c in cols) + " |", "|---|---|" + "---|" * len(cols)]
    for exp in FAULT_EXPS:
        for b in BUILDS:
            s = data["faults"].get(exp, {}).get(b)
            if not s:
                continue
            rows.append(f"| {exp} | {b} | " + " | ".join(fmt(s["mechanisms"][k], 1) for k, _ in cols) + " |")
    return rows


def table_alerts(data: dict) -> list[str]:
    rows = ["| Exp | Build | Alert required | Runs detected by a related alert | First related alert s | "
            "False-positive alerts (total) | Unrelated alerts | Alerts seen |", "|---|---|---|---|---|---|---|---|"]
    for exp in FAULT_EXPS:
        for b in BUILDS:
            s = data["faults"].get(exp, {}).get(b)
            if not s:
                continue
            rows.append(f"| {exp} | {b} | {'yes' if s['alerts_required'] else 'no'} | {s['alerts_detected']}/{s['reps']} | "
                        f"{fmt(s['first_related_alert_s'], 2)} | {s['false_positive_alerts']} | "
                        f"{', '.join(s['fp_names']) or '–'} | {', '.join(s['alerts_fired']) or '–'} |")
    return rows


def table_tradeoffs(data: dict) -> list[str]:
    rows = ["| Build | App containers | p50 ms | p95 ms | p99 ms | Core p95 ms | CPU (cores, avg) | RSS (MB) | "
            "Alerts in a fault-free run |", "|---|---|---|---|---|---|---|---|---|"]
    for b in BUILDS:
        s = data["faults"].get("X0", {}).get(b)
        if not s:
            continue
        rows.append(f"| {b} | {s['app_containers']} | {fmt(s['p50_ms'], 1)} | {fmt(s['p95_ms'], 1)} | {fmt(s['p99_ms'], 1)} | "
                    f"{fmt(s['core_p95_ms'], 1)} | {fmt(s['cpu_cores'], 2)} | {fmt(s['rss_mb'], 0)} | "
                    f"{s['false_positive_alerts']} |")
    return rows


def table_security(data: dict) -> list[str]:
    rows = ["| Exp | Build | Reps | Cases refused | Data unchanged | Audit chain ok | Denial audit records | Alerts | PASS |",
            "|---|---|---|---|---|---|---|---|---|"]
    for exp in SEC_EXPS:
        s = data["security"].get(exp)
        if s:
            rows.append(f"| {exp} | endterm | {s['reps']} | {s['cases']} (all reps: {'yes' if s['cases_all_reps'] else 'NO'}) | "
                        f"{s['data_unchanged']}/{s['reps']} | {s['audit_ok']}/{s['reps']} | {fmt(s['audit_records'], 0)} | "
                        f"{', '.join(s['alerts']) or '–'} | {s['passed']}/{s['reps']} |")
    return rows


def table_reliability(data: dict) -> list[str]:
    rows = []
    for mode in ("baseline", "endterm"):
        r = data["reliability"].get(mode)
        if not r:
            continue
        rows += [f"**{mode}** — {r['duration_s']:.0f} s, {r['rate']} req/s, crashes injected {r['kills_done']} "
                 f"(planned {r['kills_planned']})", "",
                 "| Component | Copies | Failures | Downtime s | MTTF s | MTTR s | MTBF s | A | λ (1/h) |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for k in KINDS:
            c = r["components"][k]
            rows.append(f"| {k} | {c['replicas']} | {c['failures']} | {c['downtime_s']} | {c['MTTF_s'] or '–'} | "
                        f"{c['MTTR_s'] or '–'} | {c['MTBF_s'] or '–'} | {c['availability']:.4f} | {c['failure_rate_per_h']} |")
        rows += ["", "| Service (user view) | Outages | Downtime s | MTTF s | MTTR s | A (time) | A (requests) |",
                 "|---|---|---|---|---|---|---|"]
        for k in ("student", "payment", "records", "timetable", "system"):
            s = r["services"][k]
            rows.append(f"| {k} | {s['outages']} | {s['downtime_s']} | {s['MTTF_s'] or '–'} | {s['MTTR_s'] or '–'} | "
                        f"{s['availability_time']:.4f} | {s['availability_requests']:.4f} |")
        u = r["services"]["user_level"]
        rows += ["", f"User actions: {u['requests']}, failed {u['failed']}, success {u['success']:.4f}. "
                     f"System failure rate {r['system_failure_rate_per_h']} outages per hour of up time. "
                     f"RBD prediction of system availability: {r['predicted']['system']:.4f} "
                     f"(measured {r['services']['system']['availability_time']:.4f}).", ""]
    return rows


# ----------------------------------------------------------------- SLOs and requirements

def worst(data, exps, build, key, agg="max"):
    vals = []
    for e in exps:
        s = data["faults"].get(e, {}).get(build)
        if s and s.get(key):
            vals.append(s[key][agg])
    return (max(vals) if agg == "max" else min(vals)) if vals else None


def slo_verdicts(data: dict) -> dict:
    out = {}
    for b in BUILDS:
        f = data["faults"]
        v = {}
        x0 = f.get("X0", {}).get(b)
        v["SLO-1"] = (x0["success_rate"]["min"] if x0 else None, lambda x: x is not None and x >= 0.999, "min success ratio ≥ 0.999")
        v["SLO-2"] = (worst(data, ["X1", "X2", "X3", "X8"], b, "failed_ratio"), lambda x: x is not None and x <= 0.01, "max failed ratio ≤ 1 %")
        steady = x0["core_p95_ms"]["max"] if x0 and x0["core_p95_ms"] else None
        spike = worst(data, ["X4"], b, "core_p95_ms")
        v["SLO-3"] = ((steady, spike), lambda x: x[0] is not None and x[1] is not None and x[0] <= 100 and x[1] < 1000,
                      "steady core p95 ≤ 100 ms and spike core p95 < 1000 ms")
        v["SLO-4a"] = (worst(data, ["X1", "X2", "X8"], b, "detect_event_s"), lambda x: x is not None and x <= 1.0, "max ≤ 1 s")
        det = [f.get(e, {}).get(b) for e in ("X2", "X3", "X4", "X8", "X9")]
        all_det = all(s and s["alerts_detected"] == s["reps"] for s in det)
        v["SLO-4b"] = ((all_det, worst(data, ["X2", "X3", "X4", "X8", "X9"], b, "first_related_alert_s")),
                       lambda x: x[0] and x[1] is not None and x[1] <= 10, "every run alerted, max ≤ 10 s")
        v["SLO-5"] = (worst(data, ["X1", "X2", "X8"], b, "recovery_s"), lambda x: x is not None and x <= 2.0, "max ≤ 2 s")
        cons = [(s["consistent"], s["reps"]) for e in FAULT_EXPS for s in [f.get(e, {}).get(b)] if s]
        v["SLO-6"] = ((sum(c for c, _ in cons), sum(n for _, n in cons)), lambda x: x[1] > 0 and x[0] == x[1], "100 % of runs consistent")
        if b == "endterm":
            sec = [data["security"].get(e) for e in SEC_EXPS]
            v["SLO-7"] = ((sum(s["passed"] for s in sec if s), sum(s["reps"] for s in sec if s)),
                          lambda x: x[1] > 0 and x[0] == x[1], "all security runs pass")
        else:
            v["SLO-7"] = ("n/a (no authentication)", lambda x: False, "")
        soak_r = data["reliability"].get(b)
        v["SLO-8"] = (soak_r["services"]["system"]["availability_time"] if soak_r else None,
                      lambda x: x is not None and x >= 0.995, "system availability ≥ 0.995")
        x9 = f.get("X9", {}).get(b)
        v["SLO-9"] = ((x0["false_positive_alerts"] if x0 else None, (x9["diagnosed_correctly"], x9["reps"]) if x9 else None,
                       x9["diagnosed_after_s"]["max"] if x9 and x9.get("diagnosed_after_s") else None),
                      lambda x: x[0] == 0 and x[1] is not None and x[1][0] == x[1][1] and x[2] is not None and x[2] <= 15,
                      "0 false positives in X0; gray failure attributed correctly within 15 s in every run")
        out[b] = {k: {"value": val, "met": None if val is None or isinstance(val, str) else bool(test(val)),
                      "target": tgt}
                  for k, (val, test, tgt) in v.items()}
    return out


def table_slos(slos: dict) -> list[str]:
    def show(x):
        if isinstance(x, float):
            return f"{x:.4g}"
        if isinstance(x, tuple):
            return " / ".join(show(i) for i in x)
        return str(x)
    rows = ["| SLO | Target | " + " | ".join(BUILDS) + " |", "|---|---|---|---|---|"]
    for k in slos["endterm"]:
        cells = []
        for b in BUILDS:
            e = slos[b][k]
            mark = "met" if e["met"] else ("–" if e["met"] is None else "NOT met")
            cells.append(f"{show(e['value'])} → {mark}")
        rows.append(f"| {k} | {slos['endterm'][k]['target']} | " + " | ".join(cells) + " |")
    return rows


# ----------------------------------------------------------------- figures

def fig_timelines(data: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    exps = ["X1", "X2", "X3", "X4", "X7", "X8", "X9", "X0"]
    fig, axes = plt.subplots(2, 4, figsize=(17, 7.2), sharey=True)
    for ax, exp in zip(axes.flat, exps):
        for b in BUILDS:
            rs = runs(exp, b)
            if not rs or not (Path(rs[0]["_dir"]) / "requests.csv").exists():
                continue
            s = rs[0]
            recs = load_csv(Path(s["_dir"]) / "requests.csv")
            tot, ok = per_second(recs, s["duration_s"])
            xs = [i for i, t in enumerate(tot) if t]
            ax.plot(xs, [100 * ok[i] / tot[i] for i in xs], color=COLORS[b], lw=1.5, label=b)
        ti = 10.0
        if exp == "X9":
            mit = data["faults"].get("X9", {}).get("endterm", {}).get("mitigated_after_s")
            ax.axvspan(ti, ti + (mit["mean"] if mit else 10), color="#e5e7eb", zorder=0)
        elif exp in FAULT_LEN:
            if FAULT_LEN[exp]:
                ax.axvspan(ti, ti + FAULT_LEN[exp], color="#e5e7eb", zorder=0)
            else:
                ax.axvline(ti, color="#9ca3af", lw=1, ls="--")
        ax.set_title(f"{exp} — {TITLES[exp]}", fontsize=9.5, loc="left")
        ax.set_ylim(-3, 103)
        ax.grid(alpha=0.25)
        ax.set_xlabel("time (s)", fontsize=8)
    axes[0][0].set_ylabel("% requests OK")
    axes[1][0].set_ylabel("% requests OK")
    for ax in axes.flat:                      # legend on the first panel that has data
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8, loc="lower left")
            break
    fig.suptitle("Successful requests per second, repetition 1 (grey = fault active; X9 grey = until the "
                 "runbook restarts the replica)", fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig_timelines.png", dpi=180)
    plt.close(fig)


def fig_soak(data: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    modes = [m for m in ("baseline", "endterm") if (OUT / "soak" / m / "meta.json").exists()]
    if not modes:
        return
    fig, axes = plt.subplots(len(modes), 1, figsize=(15, 3.2 * len(modes)), sharex=True)
    axes = axes if len(modes) > 1 else [axes]
    for ax, m in zip(axes, modes):
        meta = json.loads((OUT / "soak" / m / "meta.json").read_text())
        recs = load_csv(OUT / "soak" / m / "requests.csv")
        tot, ok = per_second(recs, meta["duration"])
        b = 5
        xs, ys = [], []
        for i in range(0, len(tot), b):
            t, o = sum(tot[i:i + b]), sum(ok[i:i + b])
            if t:
                xs.append(i)
                ys.append(100 * o / t)
        ax.plot(xs, ys, color=COLORS[m], lw=1.3)
        for k in meta["kills"]:
            if k["killed"]:
                ax.plot([k["t"], k["t"]], [-6, -1], color="#374151", lw=1)
        sysa = data["reliability"][m]["services"]["system"]["availability_time"]
        ax.set_title(f"{m}: {meta['kills_done']} crashes (ticks), system availability {sysa:.4f}", fontsize=10, loc="left")
        ax.set_ylim(-8, 103)
        ax.set_ylabel("% OK (5 s bins)")
        ax.grid(alpha=0.25)
    axes[-1].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(OUT / "fig_soak.png", dpi=180)
    plt.close(fig)


# ----------------------------------------------------------------- main

def main() -> None:
    import argparse
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT), help="campaign directory (default results/final)")
    OUT = Path(ap.parse_args().out)
    data = {"faults": {}, "security": {}, "reliability": {}}
    for exp in FAULT_EXPS:
        data["faults"][exp] = {b: s for b in BUILDS if (s := summarize(exp, b))}
    for exp in SEC_EXPS:
        if (s := security(exp)):
            data["security"][exp] = s
    for m in ("baseline", "endterm"):
        if (r := soak(m)):
            data["reliability"][m] = r
    slos = slo_verdicts(data)
    data["slos"] = slos

    retries = OUT / "harness_retries.jsonl"
    data["harness_retries"] = [json.loads(line) for line in retries.read_text().splitlines()] if retries.exists() else []
    for r in data["harness_retries"]:
        r.pop("trace", None)

    md = ["# Final campaign — results tables", "",
          "Generated by `python -m experiments.analyze_final` from `results/final/`. Mean ± standard deviation over "
          "repetitions; every repetition on a fresh cluster.", "",
          "## Consolidated results (fault experiments)", "", *table_results(data), "",
          "## Mechanism activations (mean per run)", "", *table_mechanisms(data), "",
          "## Alert correctness", "", *table_alerts(data), "",
          "## Trade-offs in a fault-free run (X0)", "", *table_tradeoffs(data), "",
          "## Security experiments", "", *table_security(data), "",
          "## Reliability (soak test)", "", *table_reliability(data), "",
          "## SLO verdicts", "", *table_slos(slos), "",
          f"## Harness retries: {len(data['harness_retries'])}", ""]
    for r in data["harness_retries"]:
        md.append(f"- {r.get('experiment')} {r.get('build')} rep{r.get('rep')} attempt {r.get('attempt')}: {r.get('error')}")
    (OUT / "tables.md").write_text("\n".join(md) + "\n")
    (OUT / "report_data.json").write_text(json.dumps(data, indent=1, default=str))
    fig_timelines(data)
    fig_soak(data)
    print("wrote", OUT / "tables.md", OUT / "report_data.json", "and figures")


if __name__ == "__main__":
    main()
