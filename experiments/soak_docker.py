"""Soak test on the Docker Compose cluster: steady load while application processes crash
at random, for the reliability metrics of the final project (MTTF, MTTR, MTBF,
availability, failure rate) and the comparison with the reliability-block-diagram model.

    python -m experiments.soak_docker                    # baseline and endterm, 900 s each
    python -m experiments.soak_docker --mode endterm --duration 300

Same method as the midterm soak (experiments/soak.py), now on containers:
  * every application-node instance (database nodes, the four services) gets exponentially
    distributed times to failure, mean 240 s for services and 480 s for database nodes
    (compressed time); each instance has its own seeded schedule, so node-A instances crash
    at the same moments in both builds, and the replicated build additionally loses node-B
    instances;
  * a crash is `kill -9` of every process inside the container; Docker's restart policy
    brings it back (baseline: after the emulated 20 s operator delay; endterm: at once,
    database nodes after 3 s);
  * like the midterm, a planned crash is skipped if the instance is already down (the
    monitor does not see it ready), so we never "crash" a dead process;
  * component downtime comes from the monitor's health checks (every 0.25 s); user-visible
    service downtime from the client log (1 s windows).

Writes <out>/<mode>/requests.csv, events.jsonl and meta.json (analysed by analyze_final.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from pathlib import Path

import httpx

from ftuni import config

from .dockerctl import DockerCluster
from .workload import Workload, login_all, save_csv

OUT = Path(__file__).resolve().parent.parent / "results" / "final" / "soak"
RATE = 20
MTTF = {"service": 240.0, "db": 480.0}


def schedule(mode: str, duration: float, seed: int = 2026) -> list[tuple[float, str]]:
    """Identical rule to the midterm soak, so node-A instances share one schedule across builds."""
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


async def run(mode: str, duration: float, out: Path) -> None:
    d = out / mode
    d.mkdir(parents=True, exist_ok=True)
    cl = DockerCluster(mode)
    cl.fresh()
    cl.up()
    gw = httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=5.0,
                           limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
    tokens = {}
    if config.policy(mode).auth:
        tokens = await login_all(gw, cl.creds, [f"S{i:04d}" for i in range(1, 301)])
    origin = time.time() + 1.0
    plan = schedule(mode, duration)
    kills = []
    try:
        wl = Workload(mode, RATE, duration, origin, seed=7)
        wl.tokens = tokens
        task = asyncio.create_task(wl.run(gw))
        for t, name in plan:
            await asyncio.sleep(max(0.0, origin + t - time.time()))
            up = await asyncio.to_thread(cl.prom_value, f'ftuni_monitor_target_up{{target="{name}"}}')
            if up == 1:
                await asyncio.to_thread(cl.crash, name)
            kills.append({"t": round(time.time() - origin, 3), "planned_t": t, "name": name, "killed": up == 1})
            print(f"[{mode}] t={t:6.1f}s {name}: {'crashed' if up == 1 else 'already down, skipped'}", flush=True)
        await task
        await asyncio.sleep(3)
        await gw.aclose()
        cl.export()
        events = cl.events(origin - 5)
    finally:
        cl.down()
    save_csv(wl.records, d / "requests.csv")
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (d / "meta.json").write_text(json.dumps({
        "mode": mode, "duration": duration, "rate": RATE, "mttf_injected_s": MTTF, "origin": origin,
        "kills": kills, "kills_planned": len(plan), "kills_done": sum(k["killed"] for k in kills),
        "requests": len(wl.records)}, indent=1))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "ft", "endterm", "both"], default="both")
    ap.add_argument("--duration", type=float, default=900.0)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    for mode in (["baseline", "endterm"] if args.mode == "both" else [args.mode]):
        if (Path(args.out) / mode / "meta.json").exists():
            print(f"=== soak {mode}: already done, skipped", flush=True)
            continue
        print(f"=== soak {mode} ({args.duration:.0f} s)", flush=True)
        await run(mode, args.duration, Path(args.out))


if __name__ == "__main__":
    asyncio.run(main())
