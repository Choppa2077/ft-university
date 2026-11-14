"""Final-project experiment campaign (X0-X9) on the Docker Compose cluster.

    python -m experiments.final                         # everything: X0-X4, X7-X9 x 3 builds x 5 reps, X5-X6 x 3
    python -m experiments.final X0 X9 --reps 1          # a subset
    python -m experiments.final --builds endterm        # one build

Reuses the endterm harness (experiments/endterm.py): fresh cluster per repetition ->
steady load -> inject -> evidence (requests, events, alerts, audit, Prometheus) ->
consistency check on a quiet system -> acceptance criteria. The final project adds:

  X0  steady state, no fault: latency/CPU/memory overhead and false-positive alerts
  X9  gray failure: one replica answers 50% of its requests with HTTP 500 while its
      health checks stay green, so neither the gateway nor the monitor removes it.
      Only telemetry can reveal it. An automated "on-call runbook" waits for the
      ReplicaErrorRate alert, reads the replica name from its labels and restarts that
      replica (the bad state lives in process memory). Measures detection, diagnosis
      correctness, mitigation time and user impact.

Every repetition of every build (also the X1 baseline) is run the same number of times.
The campaign can be stopped and restarted: repetitions with a summary.json are skipped.
A repetition that fails because of the test harness (e.g. the cluster does not become
ready) is retried up to twice and logged in <out>/harness_retries.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import traceback
from pathlib import Path

from . import endterm as E
from .dockerctl import DockerCluster

OUT = Path(__file__).resolve().parent.parent / "results" / "final"
GRAY_TARGET = "student-a"


# ----------------------------------------------------------------- new injections

async def inj_none(cl, wl):
    return {"fault": None}


async def inj_gray(cl, wl):
    info = {"faulty": GRAY_TARGET, "error_rate": 0.5, "t": cl.chaos(GRAY_TARGET, error_rate=0.5)}

    async def runbook():
        # the on-call response: when an alert names a replica, restart that replica
        start = time.time()
        q = 'ALERTS{alertname="ReplicaErrorRate",alertstate="firing"}'
        while time.time() - start < 30:
            res = await asyncio.to_thread(cl.prom, q)
            if res:
                info["diagnosed"] = res[0]["metric"].get("name")
                info["diagnosed_after_s"] = round(time.time() - info["t"], 3)
                await asyncio.to_thread(cl.crash, info["diagnosed"])
                info["mitigated_after_s"] = round(time.time() - info["t"], 3)
                return
            await asyncio.sleep(0.5)
        info["diagnosed"] = None                    # not detected: heal so the run can end
        await asyncio.to_thread(cl.chaos, GRAY_TARGET)

    asyncio.create_task(runbook())
    return info


def x0_accept(o):        # no failed request, no alert at all, consistent data
    return o["ok"] == o["requests"] and not o["alerts_fired"] and E.consistent_only(o)


def x9_accept(o):        # the alert names the right replica within 15 s, and data stays consistent
    inj = o.get("inject", {})
    return (inj.get("diagnosed") == GRAY_TARGET and o["detect_alert_s"] is not None
            and o["detect_alert_s"] <= 15 and E.consistent_only(o))


FAULTS = {
    "X0": dict(id="X0", title="Steady state, no fault (overhead, false-positive alerts)", duration=120, settle=3,
               inject=inj_none, watch=[], alert_names=[], detect_event=None, accept=x0_accept),
    **E.FAULTS,
    "X9": dict(id="X9", title="Gray failure: one student replica answers 50% with HTTP 500, health stays green",
               duration=55, settle=4, inject=inj_gray, watch=[GRAY_TARGET],
               alert_names=["ReplicaErrorRate", "UserFacingErrorRate"], detect_event=None, accept=x9_accept),
}
SECURITY = ["X5", "X6"]
ORDER = ["X0", "X1", "X2", "X3", "X4", "X7", "X8", "X9", "X5", "X6"]


# ----------------------------------------------------------------- driver

def log_retry(out: Path, **kw) -> None:
    with open(out / "harness_retries.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), **kw}) + "\n")


async def attempt(fn, out: Path, label: dict, tries: int = 3):
    for n in range(1, tries + 1):
        try:
            return await fn()
        except Exception as e:  # harness problem, not a system result: record it and retry on a fresh cluster
            log_retry(out, **label, attempt=n, error=f"{type(e).__name__}: {e}"[:500],
                      trace=traceback.format_exc()[-1500:])
            print(f"  harness error ({type(e).__name__}), attempt {n}/{tries}", flush=True)
            DockerCluster(label["build"]).down()
            await asyncio.sleep(10)
    raise RuntimeError(f"giving up on {label}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiments", nargs="*", help="e.g. X0 X9 (default: all)")
    ap.add_argument("--builds", nargs="+", default=None, help="subset of: baseline ft endterm")
    ap.add_argument("--reps", type=int, default=5, help="repetitions of each fault experiment")
    ap.add_argument("--sec-reps", type=int, default=3, help="repetitions of each security experiment")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    chosen = args.experiments or ORDER
    t_start = time.time()

    for exp_id in chosen:
        is_sec = exp_id in SECURITY
        builds = args.builds or (["endterm"] if is_sec else ["baseline", "ft", "endterm"])
        for build in builds:
            cl = DockerCluster(build)
            runs = []
            try:
                for rep in range(1, (args.sec_reps if is_sec else args.reps) + 1):
                    summary = out / exp_id / build / f"rep{rep}" / "summary.json"
                    if summary.exists():
                        runs.append(json.loads(summary.read_text()))
                        continue
                    label = {"experiment": exp_id, "build": build, "rep": rep}
                    if is_sec:
                        r = await attempt(lambda: E.run_security(exp_id, cl, results=out, rep=rep), out, label)
                        print(f"{exp_id} {build:8} rep{rep} PASS={r['_pass']} cases={r['cases_passed']}/"
                              f"{r['cases_total']} data_unchanged={r['data_unchanged']}", flush=True)
                    else:
                        r = await attempt(lambda: E.run_fault(FAULTS[exp_id], cl, rep, results=out), out, label)
                        print(f"{exp_id} {build:8} rep{rep} failed={r['failed']} detect_alert={r['detect_alert_s']} "
                              f"recov={r['client_visible_recovery_s']} consistent={r['consistency'].get('consistent')} "
                              f"pass={r['_pass']} [{(time.time() - t_start) / 60:.0f} min]", flush=True)
                    runs.append(r)
                if not is_sec:
                    (out / exp_id / build / "aggregate.json").write_text(json.dumps(E.aggregate(runs), indent=1))
            finally:
                cl.down()
    print(f"campaign done in {(time.time() - t_start) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
