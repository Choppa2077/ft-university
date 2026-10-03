"""Data consistency checks run after every experiment.

Money: every non-refunded bank debit must belong to exactly one COMPLETED payment,
every COMPLETED payment must have one ledger row, and no student intent may be
charged twice. Acknowledged writes must exist. In the FT build the replica must
end up with the same data as the primary.
"""
from __future__ import annotations

import asyncio
from collections import Counter

import httpx

from ftuni import config


async def _query(http: httpx.AsyncClient, url: str, sql: str, stale: bool = False) -> list[dict]:
    r = await http.post(url + "/query", json={"sql": sql, "allow_stale": stale}, timeout=5)
    d = r.json()
    return [dict(zip(d["cols"], row)) for row in d["rows"]]


async def check(mode: str, records: list) -> dict:
    async with httpx.AsyncClient() as http:
        reg = config.read_registry()
        prim = config.get(reg["db_primary"], mode)
        payments = await _query(http, prim.url, "SELECT key, status, intent, amount FROM payments")
        ledger = await _query(http, prim.url, "SELECT payment_key, amount FROM ledger")
        enroll = await _query(http, prim.url, "SELECT student_id, course_id FROM enrollments")
        debits = (await http.get(config.get("bank", mode).url + "/debits", timeout=5)).json()

        completed = {p["key"] for p in payments if p["status"] == "COMPLETED"}
        live = [d for d in debits if not d["refunded"]]
        orphan = [d["key"] for d in live if d["key"] not in completed]
        ledger_keys = {row["payment_key"] for row in ledger}
        per_intent = Counter(d["intent"] for d in live if d["intent"])
        double = {i: n for i, n in per_intent.items() if n > 1}
        stuck = [p["key"] for p in payments if p["status"] in ("PENDING", "QUEUED")]

        acked_pay = {r.intent for r in records if r.op == "pay" and r.status == 201}
        paid = {p["intent"] for p in payments if p["status"] == "COMPLETED"}
        acked_enroll = {r.detail for r in records if r.op == "enroll" and r.ok}
        enrolled = {f"{e['student_id']}:{e['course_id']}" for e in enroll}

        out = {
            "payments_completed": len(completed),
            "payments_failed": sum(p["status"] == "FAILED" for p in payments),
            "bank_debits": len(debits),
            "refunds": sum(d["refunded"] for d in debits),
            "orphan_debits": len(orphan),                       # money taken, no payment record
            "orphan_amount": sum(d["amount"] for d in live if d["key"] in set(orphan)),
            "double_charged_intents": len(double),              # student charged twice for one click
            "ledger_mismatch": len(completed ^ ledger_keys),
            "stuck_payments": len(stuck),
            "lost_acked_payments": len(acked_pay - paid),
            "lost_acked_enrollments": len(acked_enroll - enrolled),
        }

        if mode == "ft":
            out["replica"] = await replica_check(http, reg)
        ok = all(out[k] == 0 for k in ("orphan_debits", "double_charged_intents", "ledger_mismatch",
                                       "stuck_payments", "lost_acked_payments", "lost_acked_enrollments"))
        if "replica" in out:
            ok = ok and out["replica"].get("identical", False)
        out["consistent"] = ok
        return out


async def replica_check(http: httpx.AsyncClient, reg: dict) -> dict:
    prim = config.get(reg["db_primary"], "ft")
    other = next(i for i in config.of_kind("db", "ft") if i.name != prim.name)
    for _ in range(40):                      # give a returning replica time to catch up
        try:
            p = (await http.get(prim.url + "/role", timeout=1)).json()
            o = (await http.get(other.url + "/role", timeout=1)).json()
            if p["peer_state"] == "sync" and o["lsn"] == p["lsn"]:
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.25)
    else:
        return {"identical": False, "reason": "replica did not catch up"}
    sql = ("SELECT (SELECT COUNT(*) FROM payments WHERE status='COMPLETED') AS paid, "
           "(SELECT TOTAL(amount) FROM ledger) AS ledger_sum, (SELECT COUNT(*) FROM enrollments) AS enrollments, "
           "(SELECT COUNT(*) FROM payments) AS payments")
    a = (await _query(http, prim.url, sql))[0]
    b = (await _query(http, other.url, sql, stale=True))[0]
    return {"identical": a == b, "primary": prim.name, "lsn": p["lsn"], "primary_rows": a, "replica_rows": b}
