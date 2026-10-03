"""Live failure-and-recovery demo for the defence.

Start a cluster in one terminal:      python -m ftuni up --mode ft      (or --mode baseline)
Then in another terminal run e.g.:    python -m experiments.demo db

Scenarios: crash | db | node | bank | payment | load
The script sends steady traffic, injects the fault after 5 s, prints one line per
second (what users see + what the system logged) and ends with a consistency check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

from ftuni import config

from . import consistency
from .cluster import SUP, Cluster
from .workload import Workload, client

SHOW = {"DOWN", "UP", "DB_FAILOVER", "PROMOTED", "RESTART", "NODE_DOWN", "NODE_UP", "BREAKER", "UPSTREAM_DOWN",
        "UPSTREAM_UP", "RESYNCED", "REPLICA_IN_SYNC", "LOAD_SHED", "SIGNAL", "CHAOS_SET"}
COUNT_ONLY = {"PAYMENT_QUEUED", "RECOVERED", "DUPLICATE_REQUEST", "TX_INTERRUPTED", "PAYMENT_COMPLETED"}


def describe(e: dict) -> str:
    who = e.get("target") or e.get("upstream") or e.get("name") or e.get("node") or ""
    extra = ""
    if e["ev"] == "DB_FAILOVER":
        extra = f"{e['old_primary']} -> {e['new_primary']} (epoch {e['epoch']})"
    elif e["ev"] == "BREAKER":
        extra = f"{e['target']}: {e['frm']} -> {e['to']}"
    elif e["ev"] == "UP" and e.get("down_for"):
        extra = f"after {e['down_for']:.1f}s"
    return f"{e['inst']}: {e['ev']} {who} {extra}".strip()


class Tail:
    def __init__(self):
        self.pos: dict = {}
        d = config.RUN_DIR / "logs" / "events"
        for f in d.glob("*.jsonl"):
            self.pos[f] = f.stat().st_size

    def new(self) -> list[dict]:
        out = []
        for f in (config.RUN_DIR / "logs" / "events").glob("*.jsonl"):
            start = self.pos.get(f, 0)
            with open(f) as fh:
                fh.seek(start)
                data = fh.read()
                self.pos[f] = fh.tell()
            out += [json.loads(line) for line in data.splitlines() if line.strip()]
        return sorted(out, key=lambda e: e["ts"])


async def inject(name: str, cl: Cluster, http: httpx.AsyncClient) -> str:
    if name == "crash":
        await cl.kill(http, "student-a")
        return "killed student-a (SIGKILL)"
    if name == "db":
        prim = cl.db_primary()
        await cl.kill(http, prim)
        return f"killed the database primary {prim}"
    if name == "node":
        await cl.node_down(http, "A", 15)
        return "node A powered off for 15 s"
    if name == "bank":
        await cl.chaos(http, "bank", latency_ms=6000, latency_after=True, affect_health=True)
        return "bank answers after 6 s (for 12 s)"
    if name == "payment":
        for p in config.of_kind("payment", cl.mode):
            await cl.chaos(http, p.name, interrupt_rate=0.3, interrupt_at="after_debit")
        return "30% of payments die right after the bank debit (for 12 s)"
    return "burst of 120 timetable requests/s for 10 s"


async def revert(name: str, cl: Cluster, http: httpx.AsyncClient) -> None:
    if name == "bank":
        await cl.chaos(http, "bank")
    if name == "payment":
        for p in config.of_kind("payment", cl.mode):
            await cl.chaos(http, p.name)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=["crash", "db", "node", "bank", "payment", "load"])
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--rate", type=float, default=20)
    args = ap.parse_args()
    try:
        mode = httpx.get(SUP + "/ready", timeout=2).json()["mode"]
    except httpx.HTTPError:
        raise SystemExit("start the cluster first:  python -m ftuni up --mode ft")
    cl = Cluster(mode)
    tail = Tail()
    origin = time.time() + 0.5
    loads = [Workload(mode, args.rate, args.seconds, origin, seed=3)]
    if args.scenario == "load":
        loads.append(Workload(mode, 120, 10, origin, mix={"timetable": 1.0}, start_at=5, seed=4))
    counts: dict = {}
    print(f"\n{mode.upper()} build, {args.rate:.0f} req/s, fault at t=5s\n")
    async with client() as c, httpx.AsyncClient() as http:
        tasks = [asyncio.create_task(w.run(c)) for w in loads]
        injected = reverted = False
        second = 0
        while not all(t.done() for t in tasks) or second < args.seconds:
            await asyncio.sleep(max(0, origin + second + 1 - time.time()))
            second += 1
            if second == 5 and not injected:
                print(f"  >>> FAULT: {await inject(args.scenario, cl, http)}")
                injected = True
            if second == 17 and not reverted and args.scenario in ("bank", "payment"):
                await revert(args.scenario, cl, http)
                print("  >>> fault removed")
                reverted = True
            recs = [r for w in loads for r in w.records if r.t1 and second - 1 <= r.t1 < second]
            ok = sum(r.ok for r in recs)
            deg = sum(r.ok and r.degraded for r in recs)
            lat = sorted(r.latency for r in recs)
            p95 = lat[int(0.95 * (len(lat) - 1))] * 1000 if lat else 0
            evs = tail.new()
            for e in evs:
                if e["ev"] in COUNT_ONLY:
                    counts[e["ev"]] = counts.get(e["ev"], 0) + 1
            shown = [describe(e) for e in evs if e["ev"] in SHOW and not (e["ev"] == "SIGNAL")]
            bar = "#" * int(20 * ok / len(recs)) if recs else ""
            print(f"t={second:3d}s  ok {ok:3d}/{len(recs):<3d} {bar:20s} failed {len(recs) - ok:3d}  "
                  f"degraded {deg:3d}  p95 {p95:6.0f} ms  {' | '.join(shown)}")
            if second > args.seconds + 15:
                break
        await asyncio.gather(*tasks)
    print("\nwaiting 12 s for background recovery, then checking data...")
    await asyncio.sleep(12)
    records = [r for w in loads for r in w.records]
    res = await consistency.check(mode, records)
    total, ok = len(records), sum(r.ok for r in records)
    print(f"\nrequests {total}, succeeded {ok} ({100 * ok / total:.1f}%), failed {total - ok}")
    if counts:
        print("payment events:", ", ".join(f"{k.lower()} {v}" for k, v in counts.items()))
    print("orphan bank debits:", res["orphan_debits"], "| double-charged students:", res["double_charged_intents"],
          "| stuck payments:", res["stuck_payments"], "| lost acknowledged writes:",
          res["lost_acked_payments"] + res["lost_acked_enrollments"])
    if "replica" in res:
        print("replica identical to primary:", res["replica"].get("identical"))
    print("DATA CONSISTENT" if res["consistent"] else "DATA INCONSISTENT")


if __name__ == "__main__":
    asyncio.run(main())
