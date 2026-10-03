"""Pieces shared by every process: event log, chaos hooks, health endpoints."""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config


class EventLog:
    """Structured log: one JSON object per line in run/logs/events/<instance>.jsonl.
    The experiment scripts read these files to get detection/recovery timestamps."""

    def __init__(self, inst_name: str):
        self.inst = inst_name
        d = config.RUN_DIR / "logs" / "events"
        d.mkdir(parents=True, exist_ok=True)
        self.f = open(d / f"{inst_name}.jsonl", "a", buffering=1)

    def __call__(self, ev: str, **kw) -> None:
        rec = {"ts": round(time.time(), 4), "inst": self.inst, "ev": ev, **kw}
        self.f.write(json.dumps(rec) + "\n")
        print(f"{time.strftime('%H:%M:%S')} [{self.inst}] {ev} {kw if kw else ''}", file=sys.stderr, flush=True)


@dataclass
class Chaos:
    """Fault injection switches, set at runtime through POST /chaos."""
    latency_ms: int = 0
    latency_after: bool = False     # delay the response *after* the handler ran (side effects done)
    error_rate: float = 0.0         # fraction of requests answered with 500 before doing anything
    affect_health: bool = False     # apply latency/errors to health checks too
    interrupt_rate: float = 0.0     # payment service: abort a transaction half-way
    interrupt_at: str = "after_debit"


def make_app(inst: config.Instance, *,
             startup: Optional[Callable[[FastAPI], Awaitable[list]]] = None,
             ready: Optional[Callable[[], tuple[bool, dict]]] = None,
             uses_db: bool = False) -> FastAPI:
    log = EventLog(inst.name)
    pol = config.policy()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=300, max_keepalive_connections=100))
        if uses_db:
            from .dbclient import DB
            app.state.db = DB(app.state.http, pol, log)
        tasks = (await startup(app)) if startup else []
        log("STARTED", pid=os.getpid(), mode=pol.name)
        yield
        for t in tasks or []:
            t.cancel()
        await app.state.http.aclose()

    app = FastAPI(title=inst.name, lifespan=lifespan)
    app.state.inst = inst
    app.state.log = log
    app.state.policy = pol
    app.state.chaos = Chaos()

    @app.get("/health/live")
    async def live():
        return {"inst": inst.name, "status": "alive"}

    @app.get("/health/ready")
    async def readiness():
        ok, info = ready() if ready else (True, {})
        body = {"inst": inst.name, "status": "ready" if ok else "not_ready", **info}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/chaos")
    async def get_chaos():
        return asdict(app.state.chaos)

    @app.post("/chaos")
    async def set_chaos(request: Request):
        body = await request.json()
        app.state.chaos = Chaos(**body)
        log("CHAOS_SET", **body)
        return asdict(app.state.chaos)

    @app.middleware("http")
    async def chaos_middleware(request: Request, call_next):
        ch: Chaos = app.state.chaos
        path = request.url.path
        if path == "/chaos" or (path.startswith("/health") and not ch.affect_health):
            return await call_next(request)
        if ch.error_rate and random.random() < ch.error_rate:
            return JSONResponse({"error": "injected failure"}, status_code=500)
        if ch.latency_ms and not ch.latency_after:
            await asyncio.sleep(ch.latency_ms / 1000)
        response = await call_next(request)
        if ch.latency_ms and ch.latency_after:
            await asyncio.sleep(ch.latency_ms / 1000)
        return response

    return app
