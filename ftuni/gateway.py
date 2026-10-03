"""API gateway / load balancer - the single entry point for clients.

FT build:
  * every service has two replicas (node A and node B), round-robin between healthy ones
  * active health checks every 0.3 s + passive marking on connection errors
  * per-try timeout and up to 2 retries on another replica for idempotent requests
    (GET, or POST that carries an Idempotency-Key)
  * one circuit breaker per service
  * bulkhead per service: too many requests in flight -> shed immediately
  * graceful degradation: timetable answers are cached and served stale when the
    timetable service is overloaded or down
Baseline: one upstream per service, 30 s timeout, nothing else.
"""
from __future__ import annotations

import asyncio
import itertools
from collections import Counter

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import config
from .common import make_app
from .resilience import Bulkhead, CircuitBreaker, backoff_delay

ROUTES = {"students": "student", "payments": "payment", "transcripts": "records", "timetable": "timetable"}
INFLIGHT_LIMIT = {"student": 64, "payment": 48, "records": 32, "timetable": 6}
HEALTH_INTERVAL = 0.3
RETRYABLE_STATUS = {502, 503, 504}
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}


class Upstream:
    def __init__(self, inst: config.Instance):
        self.inst = inst
        self.healthy = True
        self.fails = 0


def create_app(inst: config.Instance) -> FastAPI:
    P = config.policy()
    pools = {k: [Upstream(i) for i in config.of_kind(k)] for k in set(ROUTES.values())}
    rr = {k: itertools.count() for k in pools}
    bulkheads = {k: Bulkhead(INFLIGHT_LIMIT[k]) for k in pools}
    cache: dict[str, tuple[bytes, str]] = {}
    metrics: Counter = Counter()

    async def startup(app: FastAPI):
        return [asyncio.create_task(health_loop(app))] if P.health_checks else []

    app = make_app(inst, startup=startup)
    log = app.state.log
    breakers = {k: CircuitBreaker(k, failure_threshold=8, reset_timeout=2.0,
                                  on_change=lambda n, o, s: log("BREAKER", target=n, frm=o, to=s))
                for k in pools}

    def mark(up: Upstream, ok: bool, why: str = "") -> None:
        if ok:
            up.fails = 0
            if not up.healthy:
                up.healthy = True
                log("UPSTREAM_UP", upstream=up.inst.name)
        else:
            up.fails += 1
            if up.healthy and (up.fails >= 2 or why == "connect"):
                up.healthy = False
                log("UPSTREAM_DOWN", upstream=up.inst.name, why=why)

    async def health_loop(app: FastAPI) -> None:
        http: httpx.AsyncClient = app.state.http

        async def check(up: Upstream):
            try:
                r = await http.get(up.inst.url + "/health/ready", timeout=0.5)
                mark(up, r.status_code == 200, f"http {r.status_code}")
            except httpx.ConnectError:
                mark(up, False, "connect")
            except httpx.HTTPError as e:
                mark(up, False, type(e).__name__)

        while True:
            await asyncio.gather(*(check(u) for ups in pools.values() for u in ups))
            await asyncio.sleep(HEALTH_INTERVAL)

    def pick(kind: str, tried: list[Upstream]) -> Upstream | None:
        ups = [u for u in pools[kind] if u not in tried]
        if not P.health_checks:
            return ups[0] if ups else None
        healthy = [u for u in ups if u.healthy]
        cands = healthy or ups          # nobody healthy: try anyway ("panic mode")
        if not cands:
            return None
        return cands[next(rr[kind]) % len(cands)]

    def fallback(kind: str, cache_key: str, reason: str) -> Response:
        if P.degrade and kind == "timetable" and cache_key in cache:
            metrics["degraded", kind] += 1
            body, ctype = cache[cache_key]
            return Response(body, 200, media_type=ctype, headers={"X-Degraded": f"stale-cache; {reason}"})
        metrics["unavailable", kind] += 1
        return JSONResponse({"error": "service unavailable", "service": kind, "reason": reason},
                            status_code=503, headers={"Retry-After": "1"})

    @app.get("/metrics", response_class=PlainTextResponse)
    async def prometheus_metrics():
        lines = [f'gateway_{name}_total{{service="{kind}"}} {v}' for (name, kind), v in sorted(metrics.items())]
        lines += [f'gateway_upstream_up{{instance="{u.inst.name}"}} {int(u.healthy)}'
                  for ups in pools.values() for u in ups]
        lines += [f'gateway_breaker_open{{service="{k}"}} {int(b.state != "closed")}' for k, b in breakers.items()]
        return "\n".join(lines) + "\n"

    @app.get("/upstreams")
    async def upstreams():
        return {k: [{"name": u.inst.name, "healthy": u.healthy} for u in ups] for k, ups in pools.items()}

    @app.api_route("/{svc}", methods=["GET", "POST"])
    @app.api_route("/{svc}/{path:path}", methods=["GET", "POST"])
    async def proxy(svc: str, request: Request, path: str = ""):
        kind = ROUTES.get(svc)
        if kind is None:
            return JSONResponse({"error": "unknown route"}, status_code=404)
        url_path = f"/{svc}/{path}" if path else f"/{svc}"
        cache_key = url_path + ("?" + request.url.query if request.url.query else "")
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        idempotent = request.method == "GET" or "idempotency-key" in request.headers
        metrics["requests", kind] += 1

        if P.load_shedding and not bulkheads[kind].try_acquire():
            metrics["shed", kind] += 1
            if metrics["shed", kind] == 1 or metrics["shed", kind] % 100 == 0:
                log("LOAD_SHED", service=kind, total=metrics["shed", kind])
            return fallback(kind, cache_key, "overloaded")
        try:
            if P.breaker and not breakers[kind].allow():
                return fallback(kind, cache_key, "circuit open")
            max_tries = 1 + (P.gw_retries if idempotent else 0)
            tried: list[Upstream] = []
            reason = "no upstream"
            # a refused connection means the request never left, so one extra try is
            # allowed even for non-idempotent requests
            while len(tried) < max_tries or (reason == "connect" and len(tried) == max_tries):
                up = pick(kind, tried)
                if up is None:
                    break
                if tried:
                    metrics["retries", kind] += 1
                    await asyncio.sleep(backoff_delay(len(tried) - 1, 0.02, 0.2))
                tried.append(up)
                try:
                    r = await app.state.http.request(request.method, up.inst.url + url_path,
                                                     params=request.query_params, content=body, headers=headers,
                                                     timeout=P.timeout(kind))
                except httpx.ConnectError:
                    mark(up, False, "connect")
                    reason = "connect"
                    continue                                # request never left: safe to try the next one
                except httpx.TimeoutException:
                    reason = "timeout"
                    if not idempotent:
                        break
                    continue
                except httpx.TransportError as e:            # connection dropped mid-request
                    reason = type(e).__name__
                    mark(up, False, reason)
                    if not idempotent:
                        break
                    continue
                if r.status_code in RETRYABLE_STATUS and idempotent and len(tried) < max_tries:
                    reason = f"http {r.status_code}"
                    continue
                if P.breaker:
                    (breakers[kind].failure if r.status_code >= 500 else breakers[kind].success)()
                if kind == "timetable" and r.status_code == 200 and P.degrade:
                    cache[cache_key] = (r.content, r.headers.get("content-type", "application/json"))
                out = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}
                out.update({"X-Served-By": up.inst.name, "X-Attempts": str(len(tried))})
                return Response(r.content, r.status_code, headers=out,
                                media_type=r.headers.get("content-type"))
            if P.breaker:
                breakers[kind].failure()
            if not P.health_checks and not P.degrade:
                return JSONResponse({"error": "upstream failed", "reason": reason}, status_code=502)
            return fallback(kind, cache_key, reason)
        finally:
            if P.load_shedding:
                bulkheads[kind].release()

    return app
