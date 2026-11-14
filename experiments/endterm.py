"""Endterm experiments X1-X8 on the Docker Compose cluster.

    python -m experiments.endterm                     # all experiments, default builds, 5 repetitions
    python -m experiments.endterm X1 X5 --reps 3      # a subset
    python -m experiments.endterm --builds endterm    # only one build

Each experiment: fresh cluster -> steady load -> inject a fault or run security probes
-> collect evidence from Prometheus, the alert sink, the audit log and the consistency
checks -> score against the acceptance criteria (R-ids) -> results/endterm/<exp>/<build>/.
Every fault experiment is repeated `reps` times; the report shows mean +/- spread.
Restores the system to a known state (fresh cluster) between experiments.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path

import httpx

from ftuni import config

from . import security_tests as sec
from .dockerctl import DockerCluster
from .workload import Workload, login_all, save_csv

RESULTS = Path(__file__).resolve().parent.parent / "results" / "endterm"
RATE = 40
T_INJECT = 10.0


# ----------------------------------------------------------------- fault experiments

async def steady_load(cl: DockerCluster, duration: float, origin: float, rate: float = RATE,
                      mix=None) -> Workload:
    wl = Workload(cl.mode, rate, duration, origin, mix=mix)
    gw = httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=5.0,
                           limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
    if cl.mode in ("ft", "endterm") and config.policy(cl.mode).auth:
        wl.tokens = await login_all(gw, cl.creds, [f"S{i:04d}" for i in range(1, 301)])
    asyncio.create_task(wl.run(gw))
    wl._client = gw
    return wl


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))] if values else 0.0


def detection_from_alerts(alerts, names, t_inject):
    firing = [a["received"] for a in alerts if a.get("status") == "firing"
              and a.get("alertname") in names and a["received"] >= t_inject - 0.2]
    return round(min(firing) - t_inject, 3) if firing else None


def detection_from_events(events, pred, t_inject):
    hit = [e["ts"] for e in events if e["ts"] >= t_inject - 0.2 and pred(e)]
    return round(min(hit) - t_inject, 3) if hit else None


def mechanism_counts(cl: DockerCluster, events: list[dict]) -> dict:
    """How often each fault-tolerance mechanism acted during the run (final project,
    section 7: retries, breaker activations, failovers, recovery actions). Gateway
    counters come from Prometheus (fresh cluster, so totals = this run); the rest are
    structured log events."""
    ev = Counter(e["ev"] for e in events)
    recovered = Counter(e.get("action") for e in events if e["ev"] == "RECOVERED")

    def prom_total(metric: str) -> int | None:
        try:
            v = cl.prom_value(f"sum({metric})")
        except Exception:
            return None
        return int(v) if v is not None else 0

    return {
        "gateway_retries": prom_total("ftuni_gateway_retries_total"),
        "gateway_shed": prom_total("ftuni_gateway_shed_total"),
        "gateway_stale_answers": prom_total("ftuni_gateway_degraded_total"),
        "gateway_fast_fail": prom_total("ftuni_gateway_unavailable_total"),
        "upstream_marked_down": ev["UPSTREAM_DOWN"],
        "breaker_opened": sum(1 for e in events if e["ev"] == "BREAKER" and e.get("to") == "open"),
        "bank_retries": ev["BANK_RETRY"],
        "payments_queued": ev["PAYMENT_QUEUED"],
        "recovered_roll_forward": recovered["roll_forward"],
        "recovered_roll_back": recovered["roll_back"],
        "compensations": ev["COMPENSATED"],
        "duplicate_requests": ev["DUPLICATE_REQUEST"],
        "db_failovers": ev["DB_FAILOVER"],
        "db_client_switches": ev["DB_SWITCHED"],
        "db_fenced_or_deposed": ev["FENCED"] + ev["DEPOSED"] + ev["STARTUP_DEPOSED"],
        "db_demoted": ev["DEMOTED"],      # returning old primary told to step down by the monitor
        "db_resyncs": ev["RESYNCED"],
        "monitor_down_events": ev["DOWN"],
        "container_restarts": sum(cl.inspect(n)["RestartCount"] for n in cl.names),
    }


def resource_usage(cl: DockerCluster, start: float, end: float) -> dict:
    """Average CPU (cores) and current resident memory of the application processes
    (gateway, monitor, bank, database nodes, services) over the run."""
    window = max(10, int(end - start))
    try:
        cpu = cl.prom_value(f'sum(rate(process_cpu_seconds_total{{job=~"ftuni|gateway"}}[{window}s]))')
        rss = cl.prom_value('sum(process_resident_memory_bytes{job=~"ftuni|gateway"})')
    except Exception:
        cpu = rss = None
    return {"cpu_cores": round(cpu, 3) if cpu is not None else None,
            "rss_mb": round(rss / 2 ** 20, 1) if rss is not None else None,
            "app_containers": len(cl.names)}


async def run_fault(exp: dict, cl: DockerCluster, rep: int, results: Path = RESULTS) -> dict:
    cl.fresh()
    t_up = cl.up()
    origin = time.time()
    wl = await steady_load(cl, exp["duration"], origin, rate=exp.get("rate", RATE), mix=exp.get("mix"))
    await asyncio.sleep(T_INJECT)
    t_inject = time.time()
    info = await exp["inject"](cl, wl)
    await asyncio.sleep(exp["duration"] - (time.time() - origin))
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                           and t.get_coro().__name__ == "run"], return_exceptions=True)
    await asyncio.sleep(exp.get("settle", 3))
    await wl._client.aclose()

    if getattr(wl, "_burst", None) is not None:
        wl.records += wl._burst.records   # fold the burst generator's requests into the totals
    cons = cl.consistency(wl.records)     # needs the live cluster
    cl.export()                           # copy events / alerts / audit out of the volume
    events = cl.events(origin)
    alerts = cl.alerts()
    recs = wl.records
    done = [r for r in recs if r.t1 > 0]
    after = [r for r in done if not r.ok and r.t0 >= (t_inject - origin) - 0.5]
    detect = detection_from_alerts(alerts, exp.get("alert_names", []), t_inject)
    det_ev = detection_from_events(events, exp["detect_event"], t_inject) if exp.get("detect_event") else None
    out = {
        "experiment": exp["id"], "build": cl.mode, "rep": rep, "title": exp["title"],
        "startup_s": round(t_up, 1), "requests": len(done), "ok": sum(r.ok for r in done),
        "failed": len(after), "degraded": sum(r.ok and r.degraded for r in done),
        "recovered_by_retry": sum(r.ok and (r.attempts > 1 or r.try_no > 1) for r in done),
        "success_rate": round(sum(r.ok for r in done) / max(1, len(done)), 4),
        "p50_ms": round(pct([r.latency for r in done], 50) * 1000, 1),
        "p95_ms": round(pct([r.latency for r in done], 95) * 1000, 1),
        "p99_ms": round(pct([r.latency for r in done], 99) * 1000, 1),
        "detect_alert_s": detect, "detect_event_s": det_ev,
        "client_visible_recovery_s": round(max((r.t1 for r in after), default=(t_inject - origin)) - (t_inject - origin), 3) if after else 0.0,
        "restart_count": {n: cl.inspect(n)["RestartCount"] for n in exp.get("watch", [])},
        "consistency": cons, "alerts_fired": sorted({a["alertname"] for a in alerts if a.get("status") == "firing"}),
        "inject": info,
    }
    out["core_p95_ms"] = await core_latency_p95(cl, origin, time.time())
    out["t_inject_rel"] = round(t_inject - origin, 3)
    out["duration_s"] = exp["duration"]
    out["mechanisms"] = mechanism_counts(cl, events)
    out["resources"] = resource_usage(cl, origin, time.time())
    out["alerts_detail"] = [
        {"alertname": a.get("alertname"), "status": a.get("status"),
         "labels": {k: v for k, v in (a.get("labels") or {}).items() if k in ("name", "service", "target")},
         "t_rel_inject": round(a["received"] - t_inject, 3)}
        for a in alerts if "received" in a]
    outdir = results / exp["id"] / cl.mode / f"rep{rep}"
    outdir.mkdir(parents=True, exist_ok=True)
    save_csv(recs, outdir / "requests.csv")
    (outdir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    out["_pass"] = exp["accept"](out)
    (outdir / "summary.json").write_text(json.dumps(out, indent=1))
    return out


async def core_latency_p95(cl, start, end) -> float | None:
    q = ('histogram_quantile(0.95, sum by (le) (rate(ftuni_http_request_duration_seconds_bucket'
         '{job="gateway", service=~"student|payment"}[20s])))')
    res = cl.prom_range(q, start, end)
    vals = [float(v) for _, v in (res[0]["values"] if res else []) if v not in ("NaN", "+Inf")]
    return round(max(vals) * 1000, 1) if vals else None


# injections -------------------------------------------------------

async def inj_crash(cl, wl):
    return {"killed": "payment-a", "t": cl.crash("payment-a")}

async def inj_db_pause(cl, wl):
    prim = cl.db_primary()
    cl.pause(prim)
    async def resume():
        await asyncio.sleep(12)
        cl.unpause(prim)
    asyncio.create_task(resume())
    return {"paused_primary": prim, "resume_after_s": 12}

async def inj_bank(cl, wl):
    t = cl.chaos("bank", latency_ms=6000, latency_after=True, affect_health=True)
    async def heal():
        await asyncio.sleep(15)
        cl.chaos("bank")
    asyncio.create_task(heal())
    return {"bank_delay_ms": 6000, "for_s": 15, "t": t}

async def inj_load(cl, wl):
    """Add a heavy timetable burst (like the midterm E6's second generator) on top of the
    steady load, for 15 s. The steady load keeps serving the core services so R5 (their p95
    under load) is measurable; the burst is what saturates the timetable instance(s)."""
    burst = Workload(cl.mode, rate=180, duration=15, origin=wl.origin, mix={"timetable": 1.0},
                     start_at=time.time() - wl.origin, seed=99, client_retry=False, tag="burst")
    burst.tokens = wl.tokens
    gw = httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=5.0,
                           limits=httpx.Limits(max_connections=4000, max_keepalive_connections=400))
    burst._client = gw
    wl._burst = burst

    async def run_burst():
        await burst.run(gw)
        await gw.aclose()
    asyncio.create_task(run_burst())
    return {"burst_timetable_rps": 180, "for_s": 15}

async def inj_partition(cl, wl):
    prim = cl.db_primary()
    cl.dc_network(prim, "data")
    async def heal():
        await asyncio.sleep(12)
        cl.rc_network(prim, "data")
    asyncio.create_task(heal())
    return {"partitioned": prim, "network": "data", "for_s": 12}

async def inj_interrupt(cl, wl):
    ts = [cl.chaos(p.name, interrupt_rate=0.3, interrupt_at="after_debit")
          for p in config.of_kind("payment", cl.mode)]
    async def heal():
        await asyncio.sleep(15)
        for p in config.of_kind("payment", cl.mode):
            cl.chaos(p.name)
    asyncio.create_task(heal())
    return {"interrupt_rate": 0.3, "stage": "after_debit", "for_s": 15, "t": min(ts)}


def masked_or_recovered(o):      # FT/endterm: no user requests lost and data stays consistent
    return o["failed"] == 0 and o["consistency"].get("consistent", False)

def consistent_only(o):
    return o["consistency"].get("consistent", False)


FAULTS = {
    "X1": dict(id="X1", title="Service instance failure (payment-a killed)", duration=40, settle=4,
               inject=inj_crash, watch=["payment-a"], alert_names=["InstanceDown"],
               detect_event=lambda e: e["inst"] == "monitor" and e["ev"] == "DOWN" and e.get("target") == "payment-a",
               accept=masked_or_recovered),
    "X2": dict(id="X2", title="Dependency timeout (database primary frozen 12 s)", duration=45, settle=6,
               inject=inj_db_pause, watch=["db-a", "db-b"], alert_names=["InstanceDown", "DatabaseFailover"],
               detect_event=lambda e: e["inst"] == "monitor" and e["ev"] == "DOWN" and str(e.get("target", "")).startswith("db"),
               accept=masked_or_recovered),
    "X3": dict(id="X3", title="Slow external dependency (bank 6 s for 15 s)", duration=45, settle=14,
               inject=inj_bank, watch=[], alert_names=["CircuitBreakerOpen", "InstanceDown"],
               detect_event=lambda e: e["ev"] == "BREAKER" and e.get("to") == "open",
               accept=masked_or_recovered),
    "X4": dict(id="X4", title="Load spike (+180 timetable req/s for 15 s)", duration=45, settle=4, rate=RATE,
               inject=inj_load, watch=[], alert_names=["LoadShedding", "HighLatencyP95"],
               detect_event=lambda e: e["ev"] == "LOAD_SHED",
               accept=lambda o: o["core_p95_ms"] is not None and o["core_p95_ms"] < 1000 and consistent_only(o)),
    "X7": dict(id="X7", title="Recovery and consistency (30% payments interrupted 15 s)", duration=40, settle=14,
               inject=inj_interrupt, watch=[], alert_names=["PaymentsUnfinished"],
               detect_event=lambda e: e["ev"] == "TX_INTERRUPTED",
               accept=consistent_only),
    "X8": dict(id="X8", title="Network partition (primary cut from the data network 12 s)", duration=45, settle=8,
               inject=inj_partition, watch=["db-a", "db-b"], alert_names=["InstanceDown", "DatabaseFailover", "DBReplicationLost"],
               detect_event=lambda e: e["inst"] == "monitor" and e["ev"] == "DOWN" and str(e.get("target", "")).startswith("db"),
               accept=consistent_only),
}


# ----------------------------------------------------------------- security experiments

async def run_security(exp_id: str, cl: DockerCluster, results: Path = RESULTS, rep: int | None = None) -> dict:
    cl.fresh()
    cl.up()
    before = cl.checksum()
    c = httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=10)
    t0 = time.time()
    if exp_id == "X5":
        cases = await sec.unauthorized_cases(c, cl.creds, cl)
        cases += sec.insider_cases(cl)
        probes, extra = cases, {}
    else:  # X6
        cases = await sec.malformed_cases(c, cl.creds)
        flood = await sec.abuse_flood(c, cl.creds)
        probes, extra = cases, {"abuse_flood": flood}
    await c.aclose()
    after = cl.checksum()
    await asyncio.sleep(2)
    audit_ok = cl.audit_verify().get("ok")
    cl.export()
    alerts = cl.alerts()
    audit = cl.audit_records()
    denied_events = {"X5": ("AUTHN_DENIED", "AUTHZ_DENIED", "DB_DENIED", "OPS_DENIED"),
                     "X6": ("VALIDATION_REJECTED", "RATE_LIMITED", "REQUEST_REJECTED")}[exp_id]
    data_unchanged = before == after
    all_denied = all(p["ok"] for p in probes) and (extra.get("abuse_flood", {}).get("throttled", True))
    out = {
        "experiment": exp_id, "build": cl.mode,
        "title": "Unauthorized access" if exp_id == "X5" else "Abusive / malformed requests",
        "cases": probes, "cases_passed": sum(p["ok"] for p in probes), "cases_total": len(probes),
        "extra": extra,
        "data_unchanged": data_unchanged, "checksum_before": before, "checksum_after": after,
        "audit_records_for_denials": sum(a["event"] in denied_events for a in audit),
        "audit_chain_ok": audit_ok,
        "alerts_fired": sorted({a["alertname"] for a in alerts if a.get("status") == "firing"}),
        "duration_s": round(time.time() - t0, 1),
    }
    out["_pass"] = all_denied and data_unchanged and out["audit_chain_ok"]
    out["rep"] = rep
    outdir = results / exp_id / cl.mode / (f"rep{rep}" if rep else "")
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "summary.json").write_text(json.dumps(out, indent=1))
    return out


# ----------------------------------------------------------------- driver

def aggregate(runs: list[dict]) -> dict:
    keys = ["failed", "success_rate", "p95_ms", "detect_alert_s", "detect_event_s",
            "client_visible_recovery_s", "core_p95_ms"]
    agg = {}
    for k in keys:
        vals = [r[k] for r in runs if r.get(k) is not None]
        if vals:
            agg[k] = {"mean": round(statistics.mean(vals), 3), "min": min(vals), "max": max(vals),
                      "stdev": round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0, "n": len(vals)}
    agg["passed"] = sum(bool(r.get("_pass")) for r in runs)
    agg["reps"] = len(runs)
    agg["consistent_reps"] = sum(r["consistency"].get("consistent", False) for r in runs if "consistency" in r)
    return agg


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiments", nargs="*", help="e.g. X1 X5 (default: all)")
    ap.add_argument("--builds", nargs="+", default=None, help="subset of: baseline ft endterm")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--rep-start", type=int, default=1, help="number of the first repetition")
    args = ap.parse_args()

    all_fault = list(FAULTS)
    all_sec = ["X5", "X6"]
    chosen = args.experiments or (all_fault + all_sec)
    RESULTS.mkdir(parents=True, exist_ok=True)
    index = {}

    for exp_id in chosen:
        is_sec = exp_id in all_sec
        default_builds = ["endterm"] if is_sec else ["baseline", "ft", "endterm"]
        builds = args.builds or default_builds
        index[exp_id] = {}
        for build in builds:
            cl = DockerCluster(build)
            try:
                if is_sec:
                    res = await run_security(exp_id, cl)
                    index[exp_id][build] = {"pass": res["_pass"], "cases": f"{res['cases_passed']}/{res['cases_total']}",
                                            "data_unchanged": res["data_unchanged"]}
                    print(f"{exp_id} {build:8} PASS={res['_pass']} cases={res['cases_passed']}/{res['cases_total']} "
                          f"data_unchanged={res['data_unchanged']} audit_ok={res['audit_chain_ok']}")
                else:
                    reps = 1 if build == "baseline" and exp_id in ("X1",) else args.reps
                    runs = []
                    for rep in range(args.rep_start, args.rep_start + reps):
                        r = await run_fault(FAULTS[exp_id], cl, rep)
                        runs.append(r)
                        print(f"{exp_id} {build:8} rep{rep} failed={r['failed']} "
                              f"detect_alert={r['detect_alert_s']} recov={r['client_visible_recovery_s']} "
                              f"consistent={r['consistency'].get('consistent')} pass={r['_pass']}")
                    agg = aggregate(runs)
                    (RESULTS / exp_id / build / "aggregate.json").write_text(json.dumps(agg, indent=1))
                    index[exp_id][build] = {"pass": f"{agg['passed']}/{agg['reps']}", **{
                        k: agg[k]["mean"] for k in ("failed", "detect_alert_s", "client_visible_recovery_s") if k in agg}}
            finally:
                cl.down()
        (RESULTS / "index.json").write_text(json.dumps(index, indent=1))
    print("\nindex:", json.dumps(index, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
