"""Pieces shared by every process: event log, chaos hooks, health endpoints,
request IDs, metrics, audit log."""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import config, obs
from .audit import AuditLog
from .security import ops_token_ok

ACCESS_LOG = os.environ.get("FTUNI_ACCESS_LOG", "1" if config.DOCKER else "0") == "1"


class EventLog:
    """Structured log: one JSON object per line in run/logs/events/<instance>.jsonl.
    The experiment scripts read these files to get detection/recovery timestamps.
    In containers the same JSON line also goes to stdout (docker logs)."""

    def __init__(self, inst_name: str):
        self.inst = inst_name
        d = config.RUN_DIR / "logs" / "events"
        d.mkdir(parents=True, exist_ok=True)
        self.f = open(d / f"{inst_name}.jsonl", "a", buffering=1)

    def __call__(self, ev: str, **kw) -> None:
        rec = {"ts": round(time.time(), 4), "inst": self.inst, "ev": ev, **kw}
        rid = obs.request_id.get()
        if rid and "request_id" not in rec:
            rec["request_id"] = rid
        line = json.dumps(rec)
        self.f.write(line + "\n")
        obs.EVENTS.labels(ev).inc()
        if config.DOCKER:
            print(line, flush=True)
        elif ev != "REQUEST":
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


CHAOS_FIELDS = {f.name for f in fields(Chaos)}


def identity(request: Request) -> tuple[str, str]:
    """(user id, role) as asserted by the gateway. The gateway strips these headers from
    client requests and sets them itself, so inside the cluster they can be trusted."""
    return request.headers.get("x-user-id", ""), request.headers.get("x-user-role", "")


def make_app(inst: config.Instance, *,
             startup: Optional[Callable[[FastAPI], Awaitable[list]]] = None,
             ready: Optional[Callable[[], tuple[bool, dict]]] = None,
             uses_db: bool = False,
             route_label: Optional[Callable[[Request], tuple[str, str]]] = None) -> FastAPI:
    log = EventLog(inst.name)
    pol = config.policy()
    audit = AuditLog(inst.name, enabled=pol.audit)
    ops_token = config.secret("ops_token")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=300, max_keepalive_connections=100),
            event_hooks={"request": [obs.propagate_request_id]})
        if uses_db:
            from .dbclient import DB
            app.state.db = DB(app.state.http, pol, log)
        tasks = (await startup(app)) if startup else []
        log("STARTED", pid=os.getpid(), mode=pol.name)
        yield
        for t in tasks or []:
            t.cancel()
        await app.state.http.aclose()

    app = FastAPI(title=inst.name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.inst = inst
    app.state.log = log
    app.state.audit = audit
    app.state.policy = pol
    app.state.chaos = Chaos()
    app.state.extra_metrics = None      # callable returning extra exposition lines (gateway)

    def ops_allowed(request: Request) -> bool:
        return not pol.protect_ops or ops_token_ok(request.headers, ops_token)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        """Malformed input is refused with 422 and leaves a trace (metric + audit record)."""
        fields_bad = sorted({".".join(str(x) for x in e.get("loc", ())[1:]) or "body" for e in exc.errors()})[:8]
        obs.AUTH_DENIED.labels("validation").inc()
        audit("VALIDATION_REJECTED", "denied", method=request.method, path=request.url.path[:120],
              fields=fields_bad)
        log("VALIDATION_REJECTED", path=request.url.path[:120], fields=fields_bad)
        # do not echo the offending input back (it may be huge or hostile)
        return JSONResponse({"error": "invalid request", "fields": fields_bad}, status_code=422)

    @app.get("/health/live")
    async def live():
        return {"inst": inst.name, "status": "alive"}

    @app.get("/health/ready")
    async def readiness():
        ok, info = ready() if ready else (True, {})
        body = {"inst": inst.name, "status": "ready" if ok else "not_ready", **info}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics(request: Request):
        # services sit on internal networks only; the gateway's /metrics is reachable
        # from outside, so there it needs the operator token
        if inst.kind == "gateway" and not ops_allowed(request):
            obs.AUTH_DENIED.labels("ops_token").inc()
            audit("OPS_DENIED", "denied", path="/metrics")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = generate_latest()
        if app.state.extra_metrics:
            body += app.state.extra_metrics().encode()
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/chaos")
    async def get_chaos(request: Request):
        if not ops_allowed(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return asdict(app.state.chaos)

    @app.post("/chaos")
    async def set_chaos(request: Request):
        if not ops_allowed(request):
            obs.AUTH_DENIED.labels("ops_token").inc()
            audit("CHAOS_DENIED", "denied", path="/chaos")
            log("CHAOS_DENIED")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
            app.state.chaos = Chaos(**{k: v for k, v in body.items() if k in CHAOS_FIELDS})
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"error": "bad chaos request"}, status_code=400)
        log("CHAOS_SET", **asdict(app.state.chaos))
        audit("CHAOS_SET", "allowed", **asdict(app.state.chaos))
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

    def default_label(request: Request) -> tuple[str, str]:
        route = request.scope.get("route")
        return inst.kind, getattr(route, "path", None) or "unmatched"

    label = route_label or default_label

    # registered last -> outermost: it also sees requests answered by the chaos middleware
    @app.middleware("http")
    async def observe(request: Request, call_next):
        path = request.url.path
        if path.startswith("/health") or path == "/metrics":
            return await call_next(request)
        rid = obs.new_request_id(request.headers.get("x-request-id"))
        t_rid = obs.request_id.set(rid)
        t_act = obs.actor.set(request.headers.get("x-user-id", "") if inst.kind != "gateway" else "")
        svc = inst.kind
        obs.HTTP_INFLIGHT.labels(svc).inc()
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            dt = time.perf_counter() - t0
            obs.HTTP_INFLIGHT.labels(svc).dec()
            service, route = label(request)
            obs.HTTP_REQUESTS.labels(service, route, request.method, str(status)).inc()
            obs.HTTP_LATENCY.labels(service, route).observe(dt)
            if ACCESS_LOG:
                log("REQUEST", method=request.method, route=route, service=service, status=status,
                    ms=round(dt * 1000, 1), user=obs.actor.get() or getattr(request.state, "user", None))
            obs.actor.reset(t_act)
            obs.request_id.reset(t_rid)

    return app
