"""API gateway / load balancer - the single entry point for clients.

FT build (midterm):
  * every service has two replicas (node A and node B), round-robin between healthy ones
  * active health checks every 0.3 s + passive marking on connection errors
  * per-try timeout and up to 2 retries on another replica for idempotent requests
    (GET, or POST that carries an Idempotency-Key)
  * one circuit breaker per service
  * bulkhead per service: too many requests in flight -> shed immediately
  * graceful degradation: timetable answers are cached and served stale when the
    timetable service is overloaded or down
Baseline: one upstream per service, 30 s timeout, nothing else.

Endterm build adds the security edge (policy enforcement point):
  * POST /auth/login issues a short-lived JWT; every other request needs one
  * role-based + object-level authorization (security.authorize), default deny
  * identity headers (X-User-Id/X-User-Role) are removed from client requests and set
    by the gateway itself; the client's Authorization header is not forwarded
  * per-user token bucket (429), login lockout, request body size limit (413)
  * operator endpoints (/metrics, /upstreams, /chaos) need the operator token
  * every refusal is counted (metrics) and written to the hash-chained audit log
"""
from __future__ import annotations

import asyncio
import itertools
import json
from collections import Counter

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import config, obs, security
from .common import make_app
from .resilience import Bulkhead, CircuitBreaker, backoff_delay

ROUTES = {"students": "student", "payments": "payment", "transcripts": "records", "timetable": "timetable"}
INFLIGHT_LIMIT = {"student": 64, "payment": 48, "records": 32, "timetable": 6}
HEALTH_INTERVAL = 0.3
RETRYABLE_STATUS = {502, 503, 504}
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}
# a client must never be able to assert an identity or present internal credentials
STRIPPED_PREFIXES = ("x-user-", "x-auth-", "x-ops-", "x-db-", "x-internal-")
MAX_BODY = 16 * 1024            # bytes; every legitimate request body is well below 1 KB
USER_RATE, USER_BURST = 10.0, 20.0
LOGIN_RATE, LOGIN_BURST = 100.0, 400.0


class Upstream:
    def __init__(self, inst: config.Instance):
        self.inst = inst
        self.healthy = True
        self.fails = 0


def route_label(request: Request) -> tuple[str, str]:
    """(service, route) labels for metrics; bounded cardinality."""
    seg = request.url.path.strip("/").split("/", 1)[0]
    if seg in ROUTES:
        return ROUTES[seg], "/" + seg
    if seg in ("auth", "upstreams", "chaos"):
        return "gateway", "/" + seg
    return "gateway", "other"


def create_app(inst: config.Instance) -> FastAPI:
    P = config.policy()
    pools = {k: [Upstream(i) for i in config.of_kind(k)] for k in set(ROUTES.values())}
    rr = {k: itertools.count() for k in pools}
    bulkheads = {k: Bulkhead(INFLIGHT_LIMIT[k]) for k in pools}
    cache: dict[str, tuple[bytes, str]] = {}
    metrics: Counter = Counter()
    jwt_key = config.secret("jwt_key")
    ops_token = config.secret("ops_token")
    users = security.UserStore.load(config.SECRETS_DIR / "users.json") if P.auth else None
    limiter = security.RateLimiter(USER_RATE, USER_BURST)
    login_bucket = security.TokenBucket(LOGIN_RATE, LOGIN_BURST)
    guard = security.LoginGuard()

    async def startup(app: FastAPI):
        return [asyncio.create_task(health_loop(app))] if P.health_checks else []

    app = make_app(inst, startup=startup, route_label=route_label)
    log = app.state.log
    audit = app.state.audit
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

    def extra_metrics() -> str:
        lines = [f'ftuni_gateway_{name}_total{{service="{kind}"}} {v}' for (name, kind), v in sorted(metrics.items())]
        lines += [f'ftuni_gateway_upstream_up{{upstream="{u.inst.name}",service="{u.inst.kind}"}} {int(u.healthy)}'
                  for ups in pools.values() for u in ups]
        lines += [f'ftuni_gateway_bulkhead_inflight{{service="{k}"}} {b.inflight}' for k, b in bulkheads.items()]
        return "\n".join(lines) + "\n"

    app.state.extra_metrics = extra_metrics

    def ops_allowed(request: Request) -> bool:
        return not P.protect_ops or security.ops_token_ok(request.headers, ops_token)

    @app.get("/upstreams")
    async def upstreams(request: Request):
        if not ops_allowed(request):
            obs.AUTH_DENIED.labels("ops_token").inc()
            audit("OPS_DENIED", "denied", path="/upstreams")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return {k: [{"name": u.inst.name, "healthy": u.healthy} for u in ups] for k, ups in pools.items()}

    # ------------------------------------------------------------ authentication

    @app.post("/auth/login")
    async def login(request: Request):
        if not P.auth:
            return JSONResponse({"error": "authentication is not enabled in this build"}, status_code=404)
        if not login_bucket.allow():
            obs.RATE_LIMITED.labels("login").inc()
            return JSONResponse({"error": "too many login attempts"}, status_code=429, headers={"Retry-After": "1"})
        try:
            body = json.loads(await request.body())
            username, password = str(body["username"])[:64], str(body["password"])[:256]
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "username and password required"}, status_code=400)
        obs.actor.set(username)
        if guard.locked(username):
            obs.RATE_LIMITED.labels("lockout").inc()
            audit("LOGIN", "locked", username=username)
            return JSONResponse({"error": "account temporarily locked"}, status_code=429,
                                headers={"Retry-After": str(int(guard.lockout))})
        user = await asyncio.to_thread(users.check, username, password)
        if user is None:
            locked_now = guard.failed(username)
            obs.AUTH_DENIED.labels("bad_credentials").inc()
            audit("LOGIN", "denied", username=username, locked_now=locked_now)
            log("LOGIN_FAILED", username=username, locked_now=locked_now)
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
        guard.succeeded(username)
        audit("LOGIN", "allowed", username=username, role=user.role)
        return {"access_token": security.issue_token(user.username, user.role, jwt_key),
                "token_type": "bearer", "expires_in": security.TOKEN_TTL, "role": user.role}

    def refuse(request: Request, status: int, reason: str, svc: str, **kw) -> JSONResponse:
        if status == 429:
            obs.RATE_LIMITED.labels("user").inc()
        else:
            obs.AUTH_DENIED.labels(reason).inc()
        event = {401: "AUTHN_DENIED", 403: "AUTHZ_DENIED", 413: "REQUEST_REJECTED", 400: "REQUEST_REJECTED",
                 429: "RATE_LIMITED"}[status]
        if status != 429 or kw.get("sampled"):
            audit(event, "denied", service=svc, method=request.method, path=request.url.path[:120],
                  reason=reason, **{k: v for k, v in kw.items() if k != "sampled"})
            log("ACCESS_DENIED", status=status, reason=reason, path=request.url.path[:120])
        msg = {401: "authentication required", 403: "forbidden", 413: "request body too large",
               400: "malformed request", 429: "rate limit exceeded"}[status]
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {"Retry-After": "1"} if status == 429 else {}
        return JSONResponse({"error": msg}, status_code=status, headers=headers)

    def check_access(request: Request, svc: str, path: str, body: bytes, headers: dict) -> JSONResponse | None:
        token = security.bearer(request.headers.get("authorization"))
        if token is None:
            return refuse(request, 401, "missing_token", svc)
        try:
            claims = security.verify_token(token, jwt_key)
        except security.AuthError as e:
            return refuse(request, 401, e.reason, svc)
        sub, role = claims["sub"], claims["role"]
        obs.actor.set(sub)
        request.state.user = sub
        if P.rate_limit and not limiter.allow(sub):
            n = limiter.rejected.get(sub, 0)
            return refuse(request, 429, "user_rate", svc, sampled=(n == 1 or n % 25 == 0), rejected_so_far=n)
        parsed = None
        if request.method == "POST" and body:
            try:
                parsed = json.loads(body)
            except ValueError:
                return refuse(request, 400, "invalid_json", svc)
            if not isinstance(parsed, dict):
                return refuse(request, 400, "body_not_object", svc)
        parts = [p for p in path.split("/") if p]
        allowed, rule = security.authorize(claims, request.method, svc, parts, parsed)
        if not allowed:
            return refuse(request, 403, rule, svc, role=role)
        headers["X-User-Id"], headers["X-User-Role"] = sub, role
        return None

    # ------------------------------------------------------------ proxy

    @app.api_route("/{svc}", methods=["GET", "POST"])
    @app.api_route("/{svc}/{path:path}", methods=["GET", "POST"])
    async def proxy(svc: str, request: Request, path: str = ""):
        kind = ROUTES.get(svc)
        if kind is None:
            return JSONResponse({"error": "unknown route"}, status_code=404)
        url_path = f"/{svc}/{path}" if path else f"/{svc}"
        cache_key = url_path + ("?" + request.url.query if request.url.query else "")
        if P.rate_limit:
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_BODY:
                return refuse(request, 413, "body_too_large", kind, size=int(declared))
        body = await request.body()
        if P.rate_limit and len(body) > MAX_BODY:
            return refuse(request, 413, "body_too_large", kind, size=len(body))
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_HEADERS and k.lower() != "authorization"
                   and not k.lower().startswith(STRIPPED_PREFIXES)}
        headers.pop("x-request-id", None)
        headers["X-Request-ID"] = obs.request_id.get()     # the (sanitised) ID chosen by this hop
        if P.auth:
            denied = check_access(request, svc, path, body, headers)
            if denied is not None:
                return denied
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
