"""Unit tests for the security building blocks (ftuni/security.py, ftuni/audit.py)."""
from __future__ import annotations

import time

import pytest

from ftuni import security
from ftuni.audit import AuditLog, verify_dir


def test_password_hash_roundtrip_and_reject():
    h = security.hash_password("correct horse")
    assert security.verify_password("correct horse", h)
    assert not security.verify_password("wrong", h)
    assert h != security.hash_password("correct horse")   # random salt -> different each time


def test_jwt_roundtrip():
    tok = security.issue_token("S0001", "student", "key-a")
    claims = security.verify_token(tok, "key-a")
    assert claims["sub"] == "S0001" and claims["role"] == "student"


def test_jwt_wrong_key_rejected():
    tok = security.issue_token("S0001", "student", "key-a")
    with pytest.raises(security.AuthError) as e:
        security.verify_token(tok, "key-b")
    assert e.value.reason == "bad_signature"


def test_jwt_alg_none_rejected():
    import jwt
    forged = jwt.encode({"sub": "S0001", "role": "admin", "iat": 0, "exp": 9999999999,
                         "iss": security.ISSUER}, key="", algorithm="none")
    with pytest.raises(security.AuthError):
        security.verify_token(forged, "key-a")


def test_jwt_expired_rejected():
    tok = security.issue_token("S0001", "student", "key-a", ttl=-1)
    with pytest.raises(security.AuthError) as e:
        security.verify_token(tok, "key-a")
    assert e.value.reason == "expired"


def test_jwt_tampered_role_rejected():
    tok = security.issue_token("S0001", "student", "key-a")
    head, payload, sig = tok.split(".")
    with pytest.raises(security.AuthError):            # signature no longer matches
        security.verify_token(f"{head}.{payload}x.{sig}", "key-a")


@pytest.mark.parametrize("role,method,svc,parts,body,expect", [
    ("student", "GET", "students", ["S0001"], None, True),
    ("student", "GET", "students", ["S0002"], None, False),         # IDOR blocked
    ("student", "POST", "payments", [], {"student_id": "S0001"}, True),
    ("student", "POST", "payments", [], {"student_id": "S0002"}, False),
    ("student", "POST", "transcripts", ["grades"], {"student_id": "S0001"}, False),
    ("registrar", "POST", "transcripts", ["grades"], {"student_id": "S0001"}, True),
    ("registrar", "GET", "students", ["S0009"], None, True),
    ("admin", "POST", "transcripts", ["batch", "2025F"], None, True),
    ("student", "GET", "unknown", ["x"], None, False),              # default deny
])
def test_authorize(role, method, svc, parts, body, expect):
    claims = {"role": role, "sub": "S0001"}
    allowed, _ = security.authorize(claims, method, svc, parts, body)
    assert allowed is expect


def test_rate_limiter_throttles_one_key_only():
    rl = security.RateLimiter(rate=0.0, burst=5)
    allowed = sum(rl.allow("S0002") for _ in range(20))
    assert allowed == 5                                   # burst only, refill rate 0
    assert all(rl.allow("S0003") for _ in range(5))       # a different user is unaffected


def test_login_guard_locks_after_failures():
    g = security.LoginGuard(max_failures=3, window=60, lockout=30)
    assert not g.locked("S0001")
    g.failed("S0001"); g.failed("S0001")
    assert not g.locked("S0001")
    assert g.failed("S0001") is True                      # third failure -> lock
    assert g.locked("S0001")
    g.succeeded("S0001")                                  # (a successful login clears failures)


def test_ops_token_constant_time_check():
    assert security.ops_token_ok({"x-ops-token": "secret"}, "secret")
    assert not security.ops_token_ok({"x-ops-token": "nope"}, "secret")
    assert not security.ops_token_ok({}, "secret")
    assert not security.ops_token_ok({"x-ops-token": "secret"}, "")   # no token configured -> deny


def test_audit_chain_detects_tampering(tmp_path):
    a = AuditLog("svc", enabled=True, directory=tmp_path)
    a("LOGIN", "allowed", username="S0001")
    a("PAYMENT", "allowed", key="p1", amount=1000)
    a("GRADE_CHANGED", "allowed", student_id="S0001")
    assert verify_dir(tmp_path)["ok"]

    # tamper with the middle record (change an amount) and re-verify
    p = tmp_path / "svc.jsonl"
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"amount":1000', '"amount":1')
    p.write_text("\n".join(lines) + "\n")
    res = verify_dir(tmp_path)
    assert not res["ok"]


def test_audit_chain_detects_deletion(tmp_path):
    a = AuditLog("svc", enabled=True, directory=tmp_path)
    for i in range(4):
        a("EVENT", "allowed", i=i)
    p = tmp_path / "svc.jsonl"
    lines = p.read_text().splitlines()
    del lines[1]                                          # drop a record
    p.write_text("\n".join(lines) + "\n")
    assert not verify_dir(tmp_path)["ok"]


def test_audit_survives_restart(tmp_path):
    AuditLog("svc", enabled=True, directory=tmp_path)("A", "ok")
    a2 = AuditLog("svc", enabled=True, directory=tmp_path)   # "restart": continue the chain
    a2("B", "ok")
    res = verify_dir(tmp_path)
    assert res["ok"] and res["records"] == 2
