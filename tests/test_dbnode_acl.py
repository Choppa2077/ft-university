"""The database node's least-privilege ACL (endterm): the SQLite authorizer must stop a
service from touching tables it has no business with, even if it sends the SQL directly."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ftuni import config, dbnode

TOKENS = {f"FTUNI_SECRET_DB_TOKEN_{s.upper()}": f"tok-{s}-aaaaaaaaaaaaaaaaaaaaaaaa"
          for s in ("student", "payment", "records", "monitor")}
TOKENS["FTUNI_SECRET_DB_TOKEN"] = "tok-peer-aaaaaaaaaaaaaaaaaaaaaaaa"


# conftest pins MODE=baseline at import; these tests need the endterm policy and known DB
# tokens. Apply both only for the duration of this module and restore afterwards, so other
# test files (which assume baseline and no tokens) are unaffected whatever the run order.
@pytest.fixture(autouse=True)
def _endterm_mode():
    saved_mode = config.MODE
    saved_env = {k: os.environ.get(k) for k in TOKENS}
    config.MODE = "endterm"
    os.environ.update(TOKENS)
    saved_dir = config.SECRETS_DIR
    config.SECRETS_DIR = Path(tempfile.mkdtemp(prefix="ftuni-acl-"))   # no real /run/secrets in tests
    yield
    config.SECRETS_DIR = saved_dir
    config.MODE = saved_mode
    for k, v in saved_env.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def make_client():
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    config.write_registry({"db_primary": "db-a", "epoch": 1})
    return TestClient(dbnode.create_app(config.get("db-a", "endterm")))


def q(c, token, sql):
    return c.post("/query", json={"sql": sql}, headers={"X-DB-Token": token})


def tx(c, token, sql):
    return c.post("/tx", json={"tx_id": os.urandom(6).hex(), "ops": [{"sql": sql, "params": []}]},
                  headers={"X-DB-Token": token})


def test_no_token_is_unauthenticated():
    with make_client() as c:
        assert c.post("/query", json={"sql": "SELECT 1"}).status_code == 401


def test_unknown_token_rejected():
    with make_client() as c:
        assert q(c, "tok-bogus-000000000000000000000000", "SELECT 1").status_code == 401


def test_student_cannot_read_payments():
    with make_client() as c:
        assert q(c, "tok-student-aaaaaaaaaaaaaaaaaaaaaaaa", "SELECT * FROM students").status_code == 200
        r = q(c, "tok-student-aaaaaaaaaaaaaaaaaaaaaaaa", "SELECT * FROM payments")
        assert r.status_code in (400, 403)                 # authorizer denies the read


def test_student_cannot_update_grades():
    with make_client() as c:
        r = tx(c, "tok-student-aaaaaaaaaaaaaaaaaaaaaaaa", "UPDATE grades SET grade=4.0")
        assert r.status_code in (400, 403)


def test_nobody_can_drop_a_table():
    with make_client() as c:
        for svc in ("student", "payment", "records"):
            r = tx(c, f"tok-{svc}-aaaaaaaaaaaaaaaaaaaaaaaa", "DROP TABLE ledger")
            assert r.status_code in (400, 403)


def test_payment_can_write_its_own_tables():
    with make_client() as c:
        r = tx(c, "tok-payment-aaaaaaaaaaaaaaaaaaaaaaaa",
               "INSERT INTO payments(key, student_id, amount, status) VALUES ('k','S0001',100,'PENDING')")
        assert r.status_code == 200


def test_monitor_is_read_only():
    with make_client() as c:
        assert q(c, "tok-monitor-aaaaaaaaaaaaaaaaaaaaaaaa", "SELECT COUNT(*) FROM payments").status_code == 200
        r = tx(c, "tok-monitor-aaaaaaaaaaaaaaaaaaaaaaaa", "UPDATE grades SET grade=4.0")
        assert r.status_code in (400, 403)                 # monitor may read everything, write nothing


def test_service_cannot_call_promote():
    with make_client() as c:
        r = c.post("/promote", json={"epoch": 99}, headers={"X-DB-Token": "tok-student-aaaaaaaaaaaaaaaaaaaaaaaa"})
        assert r.status_code in (401, 403)                 # only the monitor may promote
