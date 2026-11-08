"""Insider attacks: run *inside* a compromised application container (student-a).

    docker exec ftuni-student-a python -m experiments.insider <case>

The attacker controls the student service's container, so it can reach the database
network and read that container's own database credential (/run/secrets/db_token).
"""
from __future__ import annotations

import json
import sys
import uuid

import httpx

from ftuni import config

GRADE_SQL = "UPDATE grades SET grade = 4.0 WHERE student_id = 'S0001'"


def main() -> None:
    case = sys.argv[1]
    reg = config.read_registry()
    prim = config.get(reg["db_primary"])
    other = next(i for i in config.of_kind("db") if i.name != prim.name)
    stolen = {"X-DB-Token": config.secret("db_token")}       # the student service's own credential
    tx = {"tx_id": "atk-" + uuid.uuid4().hex[:8], "ops": [{"sql": GRADE_SQL, "params": []}], "min_epoch": 0}
    try:
        if case == "sql_no_token":
            r = httpx.post(prim.url + "/tx", json=tx, timeout=5)
        elif case == "sql_stolen_token":
            r = httpx.post(prim.url + "/tx", json=tx, headers=stolen, timeout=5)
        elif case == "read_payments_stolen_token":
            r = httpx.post(prim.url + "/query", json={"sql": "SELECT * FROM payments"}, headers=stolen, timeout=5)
        elif case == "drop_table_stolen_token":
            r = httpx.post(prim.url + "/tx", json={**tx, "ops": [{"sql": "DROP TABLE ledger", "params": []}]},
                           headers=stolen, timeout=5)
        elif case == "promote_replica":
            r = httpx.post(other.url + "/promote", json={"epoch": 99}, headers=stolen, timeout=5)
        elif case == "snapshot_dump":
            r = httpx.get(prim.url + "/snapshot", headers=stolen, timeout=10)
            print(json.dumps({"status": r.status_code, "bytes": len(r.content)}))
            return
        else:
            raise SystemExit(f"unknown case {case}")
        print(json.dumps({"status": r.status_code, "body": r.text[:200]}))
    except httpx.HTTPError as e:
        print(json.dumps({"status": 0, "body": type(e).__name__}))


if __name__ == "__main__":
    main()
