"""End-to-end integration tests against a running endterm cluster.

Skipped automatically unless the stack is up:
    FTUNI_MODE=endterm COMPOSE_PROFILES=replicas docker compose up -d
    python deploy/gen_secrets.py
    .venv/bin/python -m pytest tests/test_integration.py
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
SECRETS = Path(__file__).resolve().parent.parent / "deploy" / "secrets"


def _up() -> bool:
    try:
        return httpx.get(GATEWAY + "/students/x", timeout=1).status_code in (401, 403)
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="endterm cluster is not running on :8000")


@pytest.fixture(scope="module")
def creds():
    return json.loads((SECRETS / "test_credentials.json").read_text())


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=GATEWAY, timeout=10) as c:
        yield c


def token(c, creds, user):
    r = c.post("/auth/login", json={"username": user, "password": creds[user]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_required_everywhere(client):
    assert client.get("/students/S0001").status_code == 401
    assert client.post("/payments", json={"student_id": "S0001", "amount": 1}).status_code == 401


def test_wrong_password_rejected(client, creds):
    assert client.post("/auth/login", json={"username": "S0001", "password": "nope"}).status_code == 401


def test_happy_path_register_pay_transcript(client, creds):
    h = token(client, creds, "S0003")
    assert client.get("/students/S0003", headers=h).status_code == 200
    r = client.post("/students/register", json={"student_id": "S0003", "course_id": "C007"}, headers=h)
    assert r.status_code == 200
    pay = client.post("/payments", json={"student_id": "S0003", "amount": 120000},
                      headers={**h, "Idempotency-Key": "itest-pay-0003", "X-Client-Intent": "itest-0003"})
    assert pay.status_code in (200, 201) and pay.json()["status"] == "COMPLETED"
    assert client.get("/transcripts/S0003", headers=h).status_code == 200


def test_request_id_is_returned_and_echoed(client, creds):
    h = token(client, creds, "S0004")
    r = client.get("/students/S0004", headers={**h, "X-Request-ID": "itest-trace-abcdef12"})
    assert r.headers.get("x-request-id") == "itest-trace-abcdef12"


def test_idempotent_payment_is_charged_once(client, creds):
    h = token(client, creds, "S0005")
    hp = {**h, "Idempotency-Key": "itest-once-0005", "X-Client-Intent": "itest-once"}
    r1 = client.post("/payments", json={"student_id": "S0005", "amount": 50000}, headers=hp)
    r2 = client.post("/payments", json={"student_id": "S0005", "amount": 50000}, headers=hp)
    assert r1.json()["bank_ref"] == r2.json()["bank_ref"]       # same debit, not a second charge


def test_idempotency_key_bound_to_body(client, creds):
    h = token(client, creds, "S0006")
    k = {"Idempotency-Key": "itest-bind-0006", "X-Client-Intent": "itest-bind"}
    client.post("/payments", json={"student_id": "S0006", "amount": 50000}, headers={**h, **k})
    # same key, different amount -> refused (a key is a promise about one request)
    r = client.post("/payments", json={"student_id": "S0006", "amount": 999999}, headers={**h, **k})
    assert r.status_code == 422


def test_idor_blocked(client, creds):
    h = token(client, creds, "S0007")
    assert client.get("/students/S0008", headers=h).status_code == 403
    assert client.get("/transcripts/S0008", headers=h).status_code == 403


def test_role_separation_grades(client, creds):
    student = token(client, creds, "S0009")
    assert client.post("/transcripts/grades",
                       json={"student_id": "S0009", "course_id": "C001", "grade": 4.0},
                       headers=student).status_code in (403, 422)
    reg = token(client, creds, "reg1")
    r = client.post("/transcripts/grades", json={"student_id": "S0009", "course_id": "C001", "grade": 4.0},
                    headers=reg)
    assert r.status_code in (200, 404)        # allowed by role (404 only if that grade row is absent)


def test_validation_rejects_bad_amount(client, creds):
    h = token(client, creds, "S0010")
    for amount in (-1, 0, 10 ** 15):
        r = client.post("/payments", json={"student_id": "S0010", "amount": amount},
                        headers={**h, "Idempotency-Key": f"itest-bad-{amount}"})
        assert r.status_code in (400, 422)


def test_metrics_need_ops_token(client):
    assert client.get("/metrics").status_code == 403
    ops = (SECRETS / "ops_token").read_text().strip()
    r = client.get("/metrics", headers={"X-Ops-Token": ops})
    assert r.status_code == 200 and b"ftuni_http_requests_total" in r.content
