"""Monitoring service.

Polls /health/ready of every instance every 0.25 s and writes UP/DOWN events
(run/logs/events/monitor.jsonl) - the experiments read detection times from here.
An instance is DOWN after 2 failed checks in a row.

In the FT build it is also the failover controller for the database: if the
primary has failed 3 checks in a row and the replica is healthy and was in sync,
it bumps the epoch in the registry and promotes the replica.
"""
from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI

from . import config
from .common import make_app

INTERVAL = 0.25
DOWN_AFTER = 2
FAILOVER_AFTER = 3


def create_app(inst: config.Instance) -> FastAPI:
    P = config.policy()
    targets = [i for i in config.topology() if i.name != inst.name]
    state = {i.name: {"up": None, "fails": 0, "since": None, "info": {}} for i in targets}

    async def startup(app: FastAPI):
        return [asyncio.create_task(poll_loop(app))]

    app = make_app(inst, startup=startup)
    log = app.state.log

    async def check(http: httpx.AsyncClient, i: config.Instance) -> tuple[bool, dict]:
        try:
            r = await http.get(i.url + "/health/ready", timeout=INTERVAL)
            return r.status_code == 200, r.json()
        except (httpx.HTTPError, ValueError):
            return False, {}

    def update(i: config.Instance, ok: bool, info: dict, now: float) -> None:
        s = state[i.name]
        if ok:
            if s["up"] is not True:
                log("UP" if s["up"] is False else "FIRST_SEEN", target=i.name, kind=i.kind, node=i.node,
                    down_for=round(now - s["since"], 3) if s["since"] else None)
            s.update(up=True, fails=0, since=None, info=info)
            return
        s["fails"] += 1
        if s["fails"] == 1:
            s["since"] = now
        if s["fails"] >= DOWN_AFTER and s["up"] is not False:
            s["up"] = False
            log("DOWN", target=i.name, kind=i.kind, node=i.node, first_failed_check=s["since"])

    async def failover(http: httpx.AsyncClient) -> None:
        reg = config.read_registry()
        prim = reg["db_primary"]
        others = [i for i in config.of_kind("db") if i.name != prim]
        if not others:
            return
        other = others[0]
        sp, so = state[prim], state[other.name]
        # someone still acting as primary with an old epoch? tell it to step down
        if so["up"] and so["info"].get("role") == "primary" and so["info"].get("epoch", 0) < reg["epoch"]:
            await http.post(other.url + "/demote", timeout=1.0)
        if sp["fails"] < FAILOVER_AFTER or not so["up"]:
            return
        if not (so["info"].get("role") == "replica" and so["info"].get("synced")):
            return
        if sp["info"].get("peer_state") != "sync":
            # the replica may be missing acknowledged writes - promoting it would lose data
            if not sp.get("blocked_logged"):
                log("FAILOVER_BLOCKED", primary=prim, last_peer_state=sp["info"].get("peer_state"))
                sp["blocked_logged"] = True
            return
        new_epoch = reg["epoch"] + 1
        config.write_registry({"db_primary": other.name, "epoch": new_epoch})
        try:
            r = await http.post(other.url + "/promote", json={"epoch": new_epoch}, timeout=1.0)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        if not ok:
            config.write_registry(reg)       # put it back, try again on the next round
        log("DB_FAILOVER", old_primary=prim, new_primary=other.name, epoch=new_epoch, ok=ok,
            primary_down_since=sp["since"])

    async def poll_loop(app: FastAPI) -> None:
        http: httpx.AsyncClient = app.state.http
        while True:
            t0 = time.time()
            results = await asyncio.gather(*(check(http, i) for i in targets))
            for i, (ok, info) in zip(targets, results):
                update(i, ok, info, t0)
            if P.db_failover:
                try:
                    await failover(http)
                except (httpx.HTTPError, OSError, KeyError) as e:
                    log("FAILOVER_ERROR", err=str(e))
            await asyncio.sleep(max(0.0, INTERVAL - (time.time() - t0)))

    @app.get("/status")
    async def status():
        return {name: {"up": s["up"], "info": s["info"]} for name, s in state.items()}

    return app
