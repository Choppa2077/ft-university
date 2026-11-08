"""Security building blocks used by the gateway (the policy enforcement point).

* Passwords: salted PBKDF2-SHA256; the gateway only holds hashes (users.json).
* Tokens: JWT signed with HS256, short lifetime, algorithm pinned on verification
  (so "alg": "none" and algorithm-confusion tokens are rejected).
* Authorization: role-based plus object-level checks ("a student may only see and
  pay for their own records"). Anything not explicitly allowed is denied.
* Abuse protection: token bucket per user, login lockout after repeated failures.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import jwt

ALGORITHM = "HS256"
ISSUER = "ftuni-gateway"
TOKEN_TTL = 3600
PBKDF2_ITERATIONS = 20_000   # kept low so 300 synthetic users can log in quickly in experiments
ROLES = ("student", "registrar", "admin")


# ------------------------------------------------------------------ passwords / users

def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(dk.hex(), digest)


@dataclass
class User:
    username: str
    role: str
    password_hash: str


class UserStore:
    def __init__(self, users: dict[str, User]):
        self.users = users

    @classmethod
    def load(cls, path: Path) -> "UserStore":
        data = json.loads(path.read_text()) if path.exists() else {}
        return cls({u: User(u, d["role"], d["hash"]) for u, d in data.items()})

    def check(self, username: str, password: str) -> User | None:
        u = self.users.get(username)
        if u is None:
            verify_password(password, hash_password("dummy"))   # same cost: no user enumeration by timing
            return None
        return u if verify_password(password, u.password_hash) else None


# ------------------------------------------------------------------ tokens

class AuthError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def issue_token(sub: str, role: str, key: str, ttl: int = TOKEN_TTL, now: float | None = None) -> str:
    now = int(now if now is not None else time.time())
    claims = {"sub": sub, "role": role, "iat": now, "exp": now + ttl, "iss": ISSUER,
              "jti": secrets.token_hex(8)}
    return jwt.encode(claims, key, algorithm=ALGORITHM)


def verify_token(token: str, key: str) -> dict:
    try:
        claims = jwt.decode(token, key, algorithms=[ALGORITHM], issuer=ISSUER,
                            options={"require": ["exp", "iat", "sub", "role", "iss"]})
    except jwt.ExpiredSignatureError:
        raise AuthError("expired")
    except jwt.InvalidSignatureError:
        raise AuthError("bad_signature")
    except jwt.PyJWTError:
        raise AuthError("invalid_token")
    if claims.get("role") not in ROLES:
        raise AuthError("invalid_role")
    return claims


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


# ------------------------------------------------------------------ authorization

def authorize(claims: dict, method: str, svc: str, parts: list[str], body: dict | None) -> tuple[bool, str]:
    """Return (allowed, reason). parts = path segments after the service prefix.

    students     GET  /students/{sid}            own sid, registrar
                 POST /students/register         body.student_id == own sid, registrar
    payments     POST /payments                  body.student_id == own sid
                 GET  /payments/{key}            any student; the payment service checks the owner
    transcripts  GET  /transcripts/{sid}         own sid, registrar
                 POST /transcripts/grades        registrar
                 *    /transcripts/batch/{term}  registrar
    timetable    GET  /timetable/{group}         any authenticated user
    admin may do everything; everything else is denied.
    """
    role, sub = claims["role"], claims["sub"]
    if role == "admin":
        return True, "admin"
    own_body = isinstance(body, dict) and body.get("student_id") == sub

    if svc == "students":
        if method == "GET" and len(parts) == 1:
            return (role == "registrar" or parts[0] == sub), "own_record"
        if method == "POST" and parts == ["register"]:
            return (role == "registrar" or (role == "student" and own_body)), "own_enrollment"
    elif svc == "payments":
        if method == "POST" and not parts:
            return (role == "student" and own_body), "own_payment"
        if method == "GET" and len(parts) == 1:
            return True, "owner_checked_by_service"
    elif svc == "transcripts":
        if method == "GET" and len(parts) == 1:
            return (role == "registrar" or parts[0] == sub), "own_transcript"
        if parts[:1] == ["grades"] and method == "POST":
            return role == "registrar", "registrar_only"
        if parts[:1] == ["batch"]:
            return role == "registrar", "registrar_only"
    elif svc == "timetable":
        if method == "GET":
            return True, "any_user"
    return False, "no_rule"


# ------------------------------------------------------------------ abuse protection

class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self.tokens, self.t = burst, time.monotonic()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """One bucket per key (user id). Old buckets are dropped so memory stays bounded."""

    def __init__(self, rate: float, burst: float, max_keys: int = 50_000):
        self.rate, self.burst, self.max_keys = rate, burst, max_keys
        self.buckets: dict[str, TokenBucket] = {}
        self.rejected: dict[str, int] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        b = self.buckets.get(key)
        if b is None:
            if len(self.buckets) >= self.max_keys:
                self.buckets.clear()
            b = self.buckets[key] = TokenBucket(self.rate, self.burst)
        ok = b.allow(now)
        if not ok:
            self.rejected[key] = self.rejected.get(key, 0) + 1
        return ok


class LoginGuard:
    """After `max_failures` wrong passwords within `window` s an account is locked for `lockout` s."""

    def __init__(self, max_failures: int = 5, window: float = 60.0, lockout: float = 30.0):
        self.max_failures, self.window, self.lockout = max_failures, window, lockout
        self.failures: dict[str, list[float]] = {}
        self.locked_until: dict[str, float] = {}

    def locked(self, username: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.locked_until.get(username, 0.0) > now

    def failed(self, username: str, now: float | None = None) -> bool:
        """Record a failure; returns True if the account just got locked."""
        now = time.monotonic() if now is None else now
        recent = [t for t in self.failures.get(username, []) if now - t < self.window] + [now]
        self.failures[username] = recent
        if len(recent) >= self.max_failures:
            self.locked_until[username] = now + self.lockout
            self.failures[username] = []
            return True
        return False

    def succeeded(self, username: str) -> None:
        self.failures.pop(username, None)


def ops_token_ok(headers, expected: str) -> bool:
    """Operator endpoints (/chaos, /metrics on the gateway, /upstreams)."""
    if not expected:
        return False
    given = headers.get("x-ops-token") or bearer(headers.get("authorization")) or ""
    return hmac.compare_digest(given, expected)
