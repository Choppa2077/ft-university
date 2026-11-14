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


STARTUP_S = 2.0


def p95_after_startup(s: dict) -> float | None:
    """Client p95 (ms) without the first 2 s of the workload. In `endterm` the 300 logins take about
    a second after the run's clock has started, so the open-loop client then sends the overdue
    requests in one burst; this removes that harness artefact from the comparison."""
    csv = Path(s["_dir"]) / "requests.csv"
    if not csv.exists():
        return None
    recs = load_csv(csv)
    start = min((r.t0 for r in recs), default=0.0)
    lat = sorted(r.t1 - r.t0 for r in recs if r.t1 > 0 and r.t0 >= start + STARTUP_S)
    return round(lat[max(0, int(0.95 * len(lat)) - 1)] * 1000, 1) if lat else None


def startup_delay(s: dict) -> float | None:
    """Seconds between the run's clock start and the first workload request (the login phase)."""
    csv = Path(s["_dir"]) / "requests.csv"
    return round(min(r.t0 for r in load_csv(csv)), 2) if csv.exists() else None


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


SERVICE_OPS = {"student": ("student", "enroll"), "payment": ("pay",), "records": ("transcript",),
               "timetable": ("timetable",)}


def symptom_seen(s: dict, a: dict) -> bool:
    """Does the client log show the symptom a user-facing alert reports, for requests that
    started in the 13 s before it fired (10 s rate window + 3 s `for`)? Used to tell a
    collateral alert (true symptom outside the expected list, TEST_PLAN amendment) from a
    false positive. Other alert types cannot be checked from the client side."""
    ops = SERVICE_OPS.get(str((a.get("labels") or {}).get("service", "")))
    csv = Path(s["_dir"]) / "requests.csv"
    if a["alertname"] not in ("HighLatencyP95", "UserFacingErrorRate") or not ops or not csv.exists():
        return False
    fired = s.get("t_inject_rel", 10.0) + a["t_rel_inject"]
    recs = [r for r in load_csv(csv) if r.op in ops and fired - 13.0 <= r.t0 <= fired]
    if not recs:
        return False
    if a["alertname"] == "HighLatencyP95":
        lat = sorted(r.t1 - r.t0 for r in recs if r.t1 > 0)
        return bool(lat) and lat[max(0, int(0.95 * len(lat)) - 1)] > 1.0
    return sum(not r.ok for r in recs) / len(recs) > 0.05


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
    outside = [a for a in firing if not related(exp, a)]
    collateral = [a for a in outside if exp != "X0" and symptom_seen(s, a)]
    fp = [a for a in outside if a not in collateral]
    wrong = [a for a in firing if exp == "X9" and a["alertname"] == "ReplicaErrorRate"
             and (a.get("labels") or {}).get("name") != "student-a"]

    def names(alerts):
        return sorted({a["alertname"] + "{" + ",".join(str(v) for v in (a.get("labels") or {}).values()) + "}"
                       for a in alerts})
    return {"required": required(exp, build), "detected": bool(rel),
            "first_related_s": min((a["t_rel_inject"] for a in rel), default=None),
            "collateral": len(collateral), "collateral_names": names(collateral),
            "false_positives": len(fp), "fp_names": names(fp),
            "wrong_diagnosis": len(wrong), "alerts": len(firing)}


# ----------------------------------------------------------------- per experiment and build

MECH_KEYS = ["gateway_retries", "upstream_marked_down", "breaker_opened", "bank_retries", "payments_queued",
             "recovered_roll_forward", "recovered_roll_back", "compensations", "duplicate_requests",
             "db_failovers", "db_client_switches", "db_fenced_or_deposed", "db_demoted", "db_stepped_down",
             "db_resyncs", "gateway_shed", "gateway_stale_answers", "gateway_fast_fail", "container_restarts"]


def mech(s: dict, k: str):
    m = s.get("mechanisms") or {}
    if k == "db_stepped_down":       # old primary out of service: demoted by the monitor or self-fenced
        parts = [m.get("db_demoted"), m.get("db_fenced_or_deposed")]
        return None if any(p is None for p in parts) else sum(parts)
    return m.get(k)


def op_latency(rs: list[dict]) -> dict:
    """Client latency per operation in ms: the median over repetitions of each repetition's
    p50 and p95 (exact, from the client log; the gateway histogram has coarse buckets)."""
    per = {op: ([], []) for op in ("student", "enroll", "pay", "transcript", "timetable")}
    for s in rs:
        csv = Path(s["_dir"]) / "requests.csv"
        if not csv.exists():
            return {}
        recs = [r for r in load_csv(csv) if r.t1 > 0]
        for op, (p50, p95) in per.items():
            lat = sorted((r.t1 - r.t0) * 1000 for r in recs if r.op == op)
            if lat:
                p50.append(lat[max(0, int(0.50 * len(lat)) - 1)])
                p95.append(lat[max(0, int(0.95 * len(lat)) - 1)])
    return {op: {"p50": round(statistics.median(a), 1), "p95": round(statistics.median(b), 1)}
            for op, (a, b) in per.items() if a}


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
        "p95_after_startup_ms": stat([p95_after_startup(s) for s in rs]),
        "startup_delay_s": stat([startup_delay(s) for s in rs]),
        "t_inject_rel": stat([s.get("t_inject_rel") for s in rs]),
        "detect_event_s": stat([s.get("detect_event_s") for s in rs]),
        "detect_alert_s": stat([s.get("detect_alert_s") for s in rs]),
        "first_related_alert_s": stat([a["first_related_s"] for a in ae]),
        "recovery_s": stat([s["client_visible_recovery_s"] for s in rs]),
        "alerts_required": ae[0]["required"],
        "alerts_detected": sum(a["detected"] for a in ae),
        "collateral_alerts": sum(a["collateral"] for a in ae),
        "collateral_names": sorted({n for a in ae for n in a["collateral_names"]}),
        "false_positive_alerts": sum(a["false_positives"] for a in ae),
        "fp_names": sorted({n for a in ae for n in a["fp_names"]}),
        "wrong_diagnosis": sum(a["wrong_diagnosis"] for a in ae),
        "alerts_fired": sorted({n for s in rs for n in s.get("alerts_fired", [])}),
        "mechanisms": {k: stat([mech(s, k) for s in rs]) for k in MECH_KEYS},
        "cpu_cores": stat([(s.get("resources") or {}).get("cpu_cores") for s in rs]),
        "rss_mb": stat([(s.get("resources") or {}).get("rss_mb") for s in rs]),
        "app_containers": (rs[0].get("resources") or {}).get("app_containers"),
        "orphan_debits": stat([s["consistency"].get("orphan_debits") for s in rs]),
        "double_charged": stat([s["consistency"].get("double_charged_intents") for s in rs]),
        "replica_identical": sum(bool((s["consistency"].get("replica") or {}).get("identical")) for s in rs),
    }
    if exp == "X0":
        out["op_latency_ms"] = op_latency(rs)
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
            "alert_runs": {n: sum(n in s.get("alerts_fired", []) for s in rs)
                           for n in sorted({n for s in rs for n in s.get("alerts_fired", [])})},
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
            "predicted": rbd(comp["kinds"], mode),
            "double_faults": (dfs := double_faults(events, recs, meta["origin"]) if mode in config.REPLICATED else []),
            "double_fault_log": event_excerpt(events, meta, dfs[0]) if dfs else [],
            "merged_kills": merged_kills(meta)}


EXCERPT_EVENTS = ("UPSTREAM_DOWN", "UPSTREAM_UP", "DOWN", "UP", "STARTED", "PROMOTED", "DB_FAILOVER",
                  "DB_SWITCHED", "DEMOTED", "RESYNCED")


def event_excerpt(events: list[dict], meta: dict, df: dict) -> list[str]:
    """The structured events (and the harness's kills) around a double fault, verbatim."""
    lo, hi = df["start_s"] - 1.0, df["end_s"] + 1.2
    rows = [(k["t"], "harness", "kill -9", k["name"]) for k in meta.get("kills", [])
            if k.get("killed") and lo <= k["t"] <= hi]
    for e in events:
        t = e["ts"] - meta["origin"]
        if lo <= t <= hi and e["ev"] in EXCERPT_EVENTS:
            rows.append((t, e["inst"], e["ev"], str(e.get("upstream") or e.get("target") or "")))
    return [f"t={t:8.3f}  {inst:<10} {ev:<14} {what}".rstrip() for t, inst, ev, what in sorted(rows)]


def double_faults(events: list[dict], recs: list, origin: float) -> list[dict]:
    """Intervals in which the gateway saw *both* replicas of a service down — the minimal cut
    set {x-a, x-b} occurring for real — with the requests that failed meanwhile."""
    down: dict[str, list[list[float]]] = {}
    for e in events:
        if e.get("inst") != "gateway" or e["ev"] not in ("UPSTREAM_DOWN", "UPSTREAM_UP"):
            continue
        name, t = e.get("upstream", ""), e["ts"] - origin
        iv = down.setdefault(name, [])
        if e["ev"] == "UPSTREAM_DOWN" and (not iv or iv[-1][1] is not None):
            iv.append([t, None])
        elif e["ev"] == "UPSTREAM_UP" and iv and iv[-1][1] is None:
            iv[-1][1] = t
    out = []
    groups = {"student": ("student", "enroll"), "payment": ("pay",), "records": ("transcript",),
              "timetable": ("timetable",)}
    for kind, ops in groups.items():
        for a0, a1 in down.get(f"{kind}-a", []):
            for b0, b1 in down.get(f"{kind}-b", []):
                lo, hi = max(a0, b0), min(a1 if a1 is not None else 1e9, b1 if b1 is not None else 1e9)
                if hi > lo:
                    failed = sum(1 for r in recs if r.op in ops and not r.ok and lo - 0.2 <= r.t0 <= hi + 0.2)
                    out.append({"service": kind, "start_s": round(lo, 2), "end_s": round(hi, 2),
                                "duration_s": round(hi - lo, 2), "failed_requests": failed})
    return out


def merged_kills(meta: dict) -> list[dict]:
    """Planned crashes of one instance closer together than the monitor's detection time: the
    second one hits a process that is already dying, so two kills make one failure."""
    ks = sorted((k for k in meta.get("kills", []) if k.get("killed")), key=lambda k: (k["name"], k["t"]))
    return [{"name": b["name"], "t1": a["t"], "t2": b["t"], "gap_s": round(b["t"] - a["t"], 2)}
            for a, b in zip(ks, ks[1:]) if a["name"] == b["name"] and b["t"] - a["t"] < 1.0]


# Illustrative only (Section 4.3): measured repair times combined with ASSUMED real failure rates.
REAL_MTTF_DAYS = {"service": 30.0, "db": 60.0}
MANUAL_REPAIR_S = 30 * 60


def extrapolate(rel: dict) -> dict:
    year = 365 * 24 * 3600

    def system(mttr: dict, replicated: bool) -> dict:
        a_sys = 1.0
        for kind in KINDS:
            mttf = REAL_MTTF_DAYS["db" if kind == "db" else "service"] * 86400
            a = mttf / (mttf + mttr[kind])
            a_sys *= (1 - (1 - a) ** 2) if replicated else a
        down = (1 - a_sys) * year
        return {"availability": a_sys, "downtime_min_per_year": round(down / 60, 3),
                "downtime_h_per_year": round(down / 3600, 1), "downtime_ms_per_year": round(down * 1000, 3)}
    out = {"assumed_mttf_days": REAL_MTTF_DAYS, "manual_repair_s": MANUAL_REPAIR_S}
    if "baseline" in rel:
        m = {k: v["MTTR_s"] for k, v in rel["baseline"]["components"].items()}
        out["baseline_measured_repair"] = system(m, False)
        out["baseline_manual_repair"] = system({k: MANUAL_REPAIR_S for k in KINDS}, False)
    if "endterm" in rel:
        m = {k: v["MTTR_s"] for k, v in rel["endterm"]["components"].items()}
        out["endterm_measured_repair"] = system(m, True)
    return out


def derived(data: dict) -> dict:
    """Headline numbers for the abstract and the discussion, computed, not typed."""
    f = data["faults"]
    exps = [e for e in FAULT_EXPS]
    out = {"fault_runs": sum(s["reps"] for e in exps for s in f.get(e, {}).values()),
           "security_runs": sum(s["reps"] for s in data["security"].values()),
           "harness_retries": len(data.get("harness_retries", []))}
    out["total_runs"] = out["fault_runs"] + out["security_runs"]
    for b in BUILDS:
        runs_b = [f[e][b] for e in exps if b in f.get(e, {})]
        out[b] = {"passed": sum(s["passed"] for s in runs_b), "runs": sum(s["reps"] for s in runs_b),
                  "consistent": sum(s["consistent"] for s in runs_b),
                  "max_orphans": max((s["orphan_debits"]["max"] for s in runs_b if s["orphan_debits"]), default=None),
                  "max_double": max((s["double_charged"]["max"] for s in runs_b if s["double_charged"]), default=None),
                  "false_positives": sum(s["false_positive_alerts"] for s in runs_b),
                  "collateral": sum(s["collateral_alerts"] for s in runs_b)}
    req = [(f[e][b]["alerts_detected"], f[e][b]["reps"]) for e in exps for b in BUILDS
           if b in f.get(e, {}) and f[e][b]["alerts_required"]]
    out["alerts_required_runs"] = sum(n for _, n in req)
    out["alerts_detected_runs"] = sum(d for d, _ in req)
    sl = data.get("slos", {}).get("baseline", {}).get("SLO-2", {}).get("value")
    out["baseline_max_failed_pct"] = round(100 * sl, 1) if sl is not None else None
    x9 = f.get("X9", {})
    out["x9_correct"] = sum(s.get("diagnosed_correctly", 0) for s in x9.values())
    out["x9_runs"] = sum(s["reps"] for s in x9.values())
    out["soak_expected_system_down_s"] = {m: round((1 - r["predicted"]["system"]) * r["duration_s"], 2)
                                          for m, r in data["reliability"].items()}
    if "ft" in f.get("X7", {}) and "endterm" in f.get("X7", {}):
        out["alert_gap_x7_s"] = round(f["X7"]["endterm"]["first_related_alert_s"]["mean"]
                                      - f["X7"]["ft"]["first_related_alert_s"]["mean"], 2)
    if "ft" in x9 and "endterm" in x9:
        out["diag_gap_x9_s"] = round(x9["endterm"]["diagnosed_after_s"]["mean"] - x9["ft"]["diagnosed_after_s"]["mean"], 2)
    x0 = f.get("X0", {})
    if "baseline" in x0 and "endterm" in x0:
        bl, en = x0["baseline"], x0["endterm"]
        out["x0"] = {"rss_increase_pct": round(100 * (en["rss_mb"]["mean"] / bl["rss_mb"]["mean"] - 1)),
                     "cpu_increase_pct": round(100 * (en["cpu_cores"]["mean"] / bl["cpu_cores"]["mean"] - 1)),
                     "pay_p50_delta_ms": round(en["op_latency_ms"]["pay"]["p50"] - bl["op_latency_ms"]["pay"]["p50"], 1),
                     "pay_p50_repl_delta_ms": round(x0["ft"]["op_latency_ms"]["pay"]["p50"] - bl["op_latency_ms"]["pay"]["p50"], 1),
                     "pay_p50_sec_delta_ms": round(en["op_latency_ms"]["pay"]["p50"] - x0["ft"]["op_latency_ms"]["pay"]["p50"], 1)}
    return out


CONFIG_NOTES = {
    "gw_timeout": "gateway timeout per service (s)", "gw_retries": "gateway retries on another replica (idempotent requests)",
    "health_checks": "active + passive health checks at the gateway", "breaker": "circuit breakers (gateway, payment → bank)",
    "load_shedding": "bulkheads at the gateway", "degrade": "stale timetable cache, QUEUED payments",
    "db_failover": "automatic database failover (monitor)", "db_timeout": "database call timeout per try (s)",
    "db_deadline": "database client retry deadline (s)", "idempotency": "idempotency keys (client, payment, bank)",
    "bank_timeout": "bank call timeout (s)", "bank_retries": "bank retries", "recovery_worker": "payment recovery worker",
    "mirror_storage": "mirrored transcript storage + checksums", "checkpointing": "transcript batch checkpoints",
    "restart_delay": "repair delay of a service (s; in Docker only the baseline waits, emulating an operator)",
    "db_restart_delay": "repair delay of a database node (s)", "auth": "JWT authentication",
    "validation": "input validation", "rate_limit": "rate limiting, login lockout", "db_acl": "per-service database privileges",
    "protect_ops": "operator token on /chaos, /metrics, /upstreams", "bind_idempotency": "idempotency key bound to payer and amount",
    "audit": "hash-chained audit log",
}


def table_config() -> list[str]:
    from dataclasses import fields
    pols = {b: config.POLICIES[b] for b in BUILDS}

    def show(v):
        if isinstance(v, dict):
            return ", ".join(f"{k} {x:g}" for k, x in v.items())
        if isinstance(v, bool):
            return "yes" if v else "no"
        return f"{v:g}" if isinstance(v, float) else str(v)
    rows = ["| Setting | Meaning | baseline | ft | endterm |", "|---|---|---|---|---|"]
    for fl in fields(pols["baseline"]):
        vals = [getattr(pols[b], fl.name) for b in BUILDS]
        if fl.name == "name" or vals[0] == vals[1] == vals[2]:
            continue
        rows.append(f"| `{fl.name}` | {CONFIG_NOTES.get(fl.name, '')} | " + " | ".join(show(v) for v in vals) + " |")
    return rows


# ----------------------------------------------------------------- tables

def table_results(data: dict) -> list[str]:
    rows = ["| Exp | Scenario | Build | Reps | Success % | Failed req. after inj. | Failed user actions | "
            "Detect (event) s | First related alert s | Recovery s | p95 ms | Core p95 ms | "
            "Orphan debits / double charges per run | Consistent | PASS |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for exp in FAULT_EXPS:
        for b in BUILDS:
            s = data["faults"].get(exp, {}).get(b)
            if not s:
                continue
            rows.append(f"| {exp} | {TITLES[exp]} | {b} | {s['reps']} | {fmt(s['success_pct'], 2)} | "
                        f"{fmt(s['failed'], 0)} | {fmt(s['user_failed'], 0)} | {fmt(s['detect_event_s'], 2)} | "
                        f"{fmt(s['first_related_alert_s'], 2)} | {fmt(s['recovery_s'], 1)} | {fmt(s['p95_ms'], 0)} | "
                        f"{fmt(s['core_p95_ms'], 0)} | {fmt(s['orphan_debits'], 0)} / {fmt(s['double_charged'], 0)} | "
                        f"{s['consistent']}/{s['reps']} | {s['passed']}/{s['reps']} |")
    return rows


def table_mechanisms(data: dict) -> list[str]:
    cols = [("gateway_retries", "GW retries"), ("upstream_marked_down", "replica marked down"),
            ("breaker_opened", "breaker opened"), ("bank_retries", "bank retries"), ("payments_queued", "queued (202)"),
            ("recovered_roll_forward", "roll fwd"), ("recovered_roll_back", "roll back"),
            ("duplicate_requests", "duplicates caught"), ("db_failovers", "DB failovers"),
            ("db_stepped_down", "old primary stepped down"), ("gateway_shed", "shed"),
            ("gateway_stale_answers", "stale answers"),
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
    def cell(n, names):
        return f"{n} ({', '.join(names)})" if n else "0"
    rows = ["| Exp | Build | Alert required | Runs detected by a related alert | First related alert s | "
            "Collateral alerts (symptom confirmed) | False positives | Alerts seen |", "|---|---|---|---|---|---|---|---|"]
    for exp in FAULT_EXPS:
        for b in BUILDS:
            s = data["faults"].get(exp, {}).get(b)
            if not s:
                continue
            rows.append(f"| {exp} | {b} | {'yes' if s['alerts_required'] else 'no'} | {s['alerts_detected']}/{s['reps']} | "
                        f"{fmt(s['first_related_alert_s'], 2)} | {cell(s['collateral_alerts'], s['collateral_names'])} | "
                        f"{cell(s['false_positive_alerts'], s['fp_names'])} | {', '.join(s['alerts_fired']) or '–'} |")
    return rows


def table_tradeoffs(data: dict) -> list[str]:
    rows = ["| Build | App containers | p50 ms | p95 ms | p99 ms | Payment p50 / p95 ms | Registration p50 / p95 ms | "
            "Core p95 ms (histogram) | CPU (cores, avg) | RSS (MB) | Alerts in a fault-free run |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in BUILDS:
        s = data["faults"].get("X0", {}).get(b)
        if not s:
            continue
        ol = s.get("op_latency_ms") or {}

        def op(name):
            v = ol.get(name)
            return f"{v['p50']} / {v['p95']}" if v else "–"
        rows.append(f"| {b} | {s['app_containers']} | {fmt(s['p50_ms'], 1)} | {fmt(s['p95_ms'], 1)} | {fmt(s['p99_ms'], 1)} | "
                    f"{op('pay')} | {op('enroll')} | {fmt(s['core_p95_ms'], 1)} | {fmt(s['cpu_cores'], 2)} | "
                    f"{fmt(s['rss_mb'], 0)} | {s['false_positive_alerts']} |")
    return rows


def table_security(data: dict) -> list[str]:
    rows = ["| Exp | Build | Reps | Cases refused | Data unchanged | Audit chain ok | Denial audit records | Alerts | PASS |",
            "|---|---|---|---|---|---|---|---|---|"]
    for exp in SEC_EXPS:
        s = data["security"].get(exp)
        if s:
            alerts = ", ".join(f"{n} ({k}/{s['reps']} runs)" for n, k in s["alert_runs"].items()) or "–"
            rows.append(f"| {exp} | endterm | {s['reps']} | {s['cases']} (all reps: {'yes' if s['cases_all_reps'] else 'NO'}) | "
                        f"{s['data_unchanged']}/{s['reps']} | {s['audit_ok']}/{s['reps']} | {fmt(s['audit_records'], 0)} | "
                        f"{alerts} | {s['passed']}/{s['reps']} |")
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
        for m in r.get("merged_kills", []):
            rows += [f"Two planned crashes of {m['name']} {m['gap_s']} s apart (t = {m['t1']} and {m['t2']} s) hit the "
                     "same dying process and count as one failure.", ""]
        for df in r.get("double_faults", []):
            rows += [f"Both {df['service']} replicas down at once: t = {df['start_s']}–{df['end_s']} s "
                     f"({df['duration_s']} s), {df['failed_requests']} failed requests.", ""]
    return rows


def table_double_fault(data: dict) -> list[str]:
    r = data["reliability"].get("endterm") or {}
    return ["```", *r.get("double_fault_log", []), "```"] if r.get("double_fault_log") else ["(none)"]


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
            shift = s.get("t_inject_rel", 10.0) - 10.0 if exp != "X0" else 0.0   # align at the injection
            xs = [i for i, t in enumerate(tot) if t]
            ax.plot([i - shift for i in xs], [100 * ok[i] / tot[i] for i in xs], color=COLORS[b], lw=1.5, label=b)
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
    fig.suptitle("Successful requests per second, repetition 1, aligned at the injection (t = 10 s; grey = fault "
                 "active; X9 grey = until the runbook restarts the replica)", fontsize=11, x=0.01, ha="left")
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
    data["extrapolation"] = extrapolate(data["reliability"])

    retries = OUT / "harness_retries.jsonl"
    data["harness_retries"] = [json.loads(line) for line in retries.read_text().splitlines()] if retries.exists() else []
    for r in data["harness_retries"]:
        r.pop("trace", None)
    data["derived"] = derived(data)

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
          "## Configuration difference between the builds", "", *table_config(), "",
          "## Double fault in the soak (event log excerpt)", "", *table_double_fault(data), "",
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
