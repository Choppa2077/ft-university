"""Steady demo traffic: prints one line/sec with ok/failed counts (for the demo video).

    python -m experiments.demo_traffic            # endterm build (default)
    python -m experiments.demo_traffic baseline   # the single-instance baseline

Ctrl+C stops it and prints a summary of the whole run.
"""
from __future__ import annotations
import asyncio, sys, time, httpx
from collections import Counter
from experiments.workload import Workload, login_all
from experiments.dockerctl import DockerCluster

async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "endterm"
    cl = DockerCluster(mode)
    gw = httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=5.0,
                           limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
    wl = Workload(mode, rate=30, duration=100000, origin=time.time())
    if mode in ("ft", "endterm"):
        print("logging in 300 students…", flush=True)
        wl.tokens = await login_all(gw, cl.creds, [f"S{i:04d}" for i in range(1, 301)])
    asyncio.create_task(wl.run(gw))
    seen = 0
    try:
        while True:
            await asyncio.sleep(1.0)
            done = [r for r in wl.records if r.t1 > 0]
            new = done[seen:]; seen = len(done)
            ok = sum(r.ok for r in new); bad = len(new) - ok
            deg = sum(r.ok and r.degraded for r in new)
            bar = "#" * ok + ("!" * bad)
            print(f"{time.strftime('%H:%M:%S')}  ok={ok:3d} failed={bad:3d} degraded={deg:3d}  {bar}", flush=True)
    except asyncio.CancelledError:
        # Ctrl+C: print a summary of the whole run instead of a traceback
        done = [r for r in wl.records if r.t1 > 0]
        ok = sum(r.ok for r in done)
        deg = sum(r.ok and r.degraded for r in done)
        print(f"\ntraffic stopped: {len(done)} requests, {ok} ok, {len(done) - ok} failed, {deg} degraded",
              flush=True)
        raise


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
