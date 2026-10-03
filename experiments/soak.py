"""Soak test: steady load for several minutes while processes crash at random.

Every application-node process gets exponentially distributed times to failure
(mean 240 s for services, 480 s for database nodes - compressed time). Node-A
processes use the same random schedule in both builds, so the baseline and the FT
build see the same crashes; the FT build additionally loses node-B processes.

From the monitor log we get per-component MTTF / MTTR / MTBF / availability /
failure rate; from the client log we get what users actually experienced.

    python -m experiments.soak --mode both
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

import httpx

from ftuni import config

from .cluster import Cluster
from .run_experiments import RESULTS
from .workload import Workload, client, save_csv

DURATION = 540
RATE = 20
MTTF = {"service": 240.0, "db": 480.0}


def schedule(mode: str, duration: float = DURATION, seed: int = 2026) -> list[tuple[float, str]]:
    plan = []
    for inst in config.topology(mode):
        if inst.node not in ("A", "B"):
            continue
        rnd = random.Random(f"{seed}-{inst.name}")
        mean = MTTF["db"] if inst.kind == "db" else MTTF["service"]
        t = 15 + rnd.expovariate(1 / mean)
        while t < duration - 30:
            plan.append((round(t, 2), inst.name))
            t += rnd.expovariate(1 / mean)
    return sorted(plan)


async def run(mode: str) -> None:
    outdir = RESULTS / f"soak_{mode}"
    outdir.mkdir(parents=True, exist_ok=True)
    cl = Cluster(mode)
    cl.start()
    origin = time.time() + 1.0
    plan = schedule(mode)
    done = []
    try:
        wl = Workload(mode, RATE, DURATION, origin, seed=7)
        async with client() as c, httpx.AsyncClient() as http:
            task = asyncio.create_task(wl.run(c))
            for t, name in plan:
                await asyncio.sleep(max(0, origin + t - time.time()))
                r = await http.post(f"http://127.0.0.1:{config.SUPERVISOR_PORT}/kill/{name}", timeout=2)
                done.append({"t": round(time.time() - origin, 3), "name": name, "killed": r.status_code == 200})
                print(f"t={t:6.1f}s kill {name}: {'ok' if r.status_code == 200 else 'already down'}", flush=True)
            await task
        await asyncio.sleep(3)
        events = cl.events()
    finally:
        cl.stop()
    save_csv(wl.records, outdir / "requests.csv")
    (outdir / "events.jsonl").write_text("\n".join(json.dumps({**e, "t": round(e["ts"] - origin, 3)})
                                                   for e in events) + "\n")
    (outdir / "meta.json").write_text(json.dumps({"mode": mode, "duration": DURATION, "rate": RATE,
                                                  "mttf_injected": MTTF, "origin": origin, "kills": done}, indent=2))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["ft", "baseline", "both"], default="both")
    args = ap.parse_args()
    for mode in (["baseline", "ft"] if args.mode == "both" else [args.mode]):
        print(f"=== soak {mode}", flush=True)
        await run(mode)


if __name__ == "__main__":
    asyncio.run(main())
