"""Payment service: tuition payments through the external bank.

FT version, in order:
  1. Idempotency-Key from the client identifies the payment. A repeated key never
     creates a second payment - it returns (or finishes) the first one.
  2. Before touching the bank we write a PENDING row. This is our checkpoint: if
     the process dies half-way, the row tells the recovery worker what was going on.
  3. Bank call: 0.8 s timeout, 2 retries with jittered backoff, circuit breaker.
     The same key is sent to the bank every time, so a retry after a timeout
     cannot charge twice.
  4. Bank down -> graceful degradation: the payment is accepted as QUEUED (HTTP 202)
     and the recovery worker finishes it when the bank is back.
  5. COMPLETED status and the ledger entry are written in one DB transaction.

Recovery rules for rows nobody is working on any more:
  * money already left the student's account (bank has the debit) -> roll forward
  * no debit and the intent is older than PENDING_EXPIRY            -> roll back (FAILED)
  * a debit exists for a payment that was rolled back                -> refund (compensation)
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from .. import config
from ..common import make_app
from ..dbclient import DBUnavailable
from ..resilience import CircuitBreaker, backoff_delay

PENDING_EXPIRY = 10.0
RECOVERY_AGE = 2.0


class PayReq(BaseModel):
    student_id: str
    amount: int


class BankUnavailable(Exception):
    pass


def create_app(inst: config.Instance) -> FastAPI:
    P = config.policy()
    BANK = config.get("bank").url

    async def startup(app: FastAPI):
        return [asyncio.create_task(recovery_loop())] if P.recovery_worker else []

    app = make_app(inst, startup=startup, uses_db=True)
    log = app.state.log
    breaker = CircuitBreaker("bank", failure_threshold=5, reset_timeout=3.0,
                             on_change=lambda name, old, new: log("BREAKER", target=name, frm=old, to=new))

    def db():
        return app.state.db

    def http() -> httpx.AsyncClient:
        return app.state.http

    def maybe_interrupt(stage: str, key: str) -> None:
        # Chaos hook for experiment E5: the worker "dies" in the middle of the transaction.
        ch = app.state.chaos
        if ch.interrupt_rate and ch.interrupt_at == stage and random.random() < ch.interrupt_rate:
            log("TX_INTERRUPTED", key=key, stage=stage)
            raise HTTPException(500, f"transaction interrupted ({stage})")

    async def bank_debit(key: str, amount: int, sid: str, intent: str) -> str:
        payload = {"key": key, "student_id": sid, "amount": amount, "intent": intent}
        if not P.idempotency:
            # baseline: one call, long timeout, errors go straight back to the user
            try:
                r = await http().post(BANK + "/debit", json=payload, timeout=P.bank_timeout)
            except httpx.TimeoutException:
                raise HTTPException(504, "bank timeout")
            except httpx.HTTPError as e:
                raise HTTPException(502, f"bank unreachable ({type(e).__name__})")
            if r.status_code != 200:
                raise HTTPException(502, f"bank error {r.status_code}")
            return r.json()["ref"]

        if not breaker.allow():
            raise BankUnavailable("circuit open")
        for attempt in range(P.bank_retries + 1):
            try:
                r = await http().post(BANK + "/debit", json=payload, timeout=P.bank_timeout)
                if r.status_code == 200:
                    breaker.success()
                    return r.json()["ref"]
                why = f"http {r.status_code}"
            except httpx.HTTPError as e:
                why = type(e).__name__
            breaker.failure()
            if attempt == P.bank_retries or not breaker.allow():
                raise BankUnavailable(why)
            log("BANK_RETRY", key=key, attempt=attempt + 1, why=why)
            await asyncio.sleep(backoff_delay(attempt, 0.1, 1.0))
        raise BankUnavailable("unreachable")

    async def get_row(key: str) -> dict | None:
        rows = await db().query("SELECT * FROM payments WHERE key=?", key)
        return rows[0] if rows else None

    async def complete(key: str, ref: str, origin: str) -> str:
        now = time.time()
        res = await db().tx(
            ("UPDATE payments SET status='COMPLETED', bank_ref=?, updated=? "
             "WHERE key=? AND status IN ('PENDING','QUEUED')", (ref, now, key)),
            ("INSERT OR IGNORE INTO ledger SELECT key, student_id, amount, ? FROM payments "
             "WHERE key=? AND status='COMPLETED'", (now, key)))
        if res[0] == 1:
            log("PAYMENT_COMPLETED", key=key, origin=origin)
            return "COMPLETED"
        row = await get_row(key)
        if row["status"] == "FAILED":
            # the intent was rolled back while we were at the bank -> give the money back
            await refund(key, origin)
        return row["status"]

    async def refund(key: str, origin: str) -> None:
        r = await http().post(BANK + f"/refund/{key}", timeout=2.0)
        if r.status_code == 200:
            log("COMPENSATED", key=key, origin=origin)

    async def settle(key: str, origin: str, response: Response | None) -> dict:
        """Drive a payment to COMPLETED, or to QUEUED if the bank cannot be reached."""
        row = await get_row(key)
        if row["status"] == "COMPLETED":
            return {"status": "COMPLETED", "key": key, "bank_ref": row["bank_ref"]}
        if row["status"] == "FAILED":
            raise HTTPException(409, {"status": "FAILED", "key": key, "note": row["note"]})
        try:
            ref = await bank_debit(key, row["amount"], row["student_id"], row["intent"])
        except BankUnavailable as e:
            await db().tx(("UPDATE payments SET status='QUEUED', updated=? WHERE key=? AND status='PENDING'",
                           (time.time(), key)))
            if row["status"] == "PENDING":
                log("PAYMENT_QUEUED", key=key, why=str(e))
            if response is not None:
                response.status_code = 202
                response.headers["X-Degraded"] = "queued"
            return {"status": "QUEUED", "key": key}
        maybe_interrupt("after_debit", key)
        status = await complete(key, ref, origin)
        if status == "FAILED":
            raise HTTPException(409, {"status": "FAILED", "key": key, "note": "rolled back"})
        return {"status": status, "key": key, "bank_ref": ref}

    async def pay_plain(req: PayReq, intent: str) -> dict:
        """No idempotency key: every call is a brand new payment (the baseline behaviour)."""
        key = "p-" + uuid.uuid4().hex[:20]
        ref = await bank_debit(key, req.amount, req.student_id, intent)
        maybe_interrupt("after_debit", key)
        now = time.time()
        await db().tx(
            ("INSERT INTO payments(key, student_id, amount, status, bank_ref, intent, created, updated) "
             "VALUES (?,?,?,'COMPLETED',?,?,?,?)", (key, req.student_id, req.amount, ref, intent, now, now)),
            ("INSERT INTO ledger VALUES (?,?,?,?)", (key, req.student_id, req.amount, now)))
        return {"status": "COMPLETED", "key": key, "bank_ref": ref}

    @app.post("/payments", status_code=201)
    async def pay(req: PayReq, request: Request, response: Response):
        intent = request.headers.get("x-client-intent", "")
        key = request.headers.get("idempotency-key")
        try:
            if not P.idempotency or not key:
                return await pay_plain(req, intent)
            if await get_row(key) is not None:
                # same key again: the client is retrying. Finish/return the original payment.
                response.headers["X-Idempotent-Replay"] = "true"
                log("DUPLICATE_REQUEST", key=key)
                return await settle(key, "client_retry", response)
            now = time.time()
            await db().tx(("INSERT OR IGNORE INTO payments(key, student_id, amount, status, intent, created, updated) "
                           "VALUES (?,?,?,'PENDING',?,?,?)", (key, req.student_id, req.amount, intent, now, now)))
            maybe_interrupt("before_debit", key)
            return await settle(key, "request", response)
        except DBUnavailable as e:
            raise HTTPException(503, f"database unavailable ({e})")

    @app.get("/payments/{key}")
    async def get_payment(key: str):
        try:
            row = await get_row(key)
        except DBUnavailable as e:
            raise HTTPException(503, f"database unavailable ({e})")
        if row is None:
            raise HTTPException(404, "no such payment")
        return row

    async def recover_one(row: dict) -> None:
        key = row["key"]
        if row["status"] == "QUEUED":
            try:
                await settle(key, "recovery_worker", None)   # roll forward once the bank answers
            except HTTPException:
                pass
            return
        try:
            r = await http().get(BANK + f"/debit/{key}", timeout=1.0)
        except httpx.HTTPError:
            return
        if r.status_code == 200:
            if await complete(key, r.json()["ref"], "recovery_worker") == "COMPLETED":
                log("RECOVERED", key=key, action="roll_forward")
        elif r.status_code == 404 and time.time() - row["created"] > PENDING_EXPIRY:
            res = await db().tx(("UPDATE payments SET status='FAILED', note='rolled back: no debit, client gone', "
                                 "updated=? WHERE key=? AND status='PENDING'", (time.time(), key)))
            if res[0]:
                log("RECOVERED", key=key, action="roll_back")

    async def reconcile_failed() -> None:
        rows = await db().query("SELECT key FROM payments WHERE status='FAILED' AND updated > ?", time.time() - 120)
        for row in rows:
            r = await http().get(BANK + f"/debit/{row['key']}", timeout=1.0)
            if r.status_code == 200 and not r.json()["refunded"]:
                await refund(row["key"], "reconciliation")

    async def recovery_loop() -> None:
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            tick += 1
            try:
                rows = await db().query("SELECT key, status, created FROM payments "
                                        "WHERE status IN ('PENDING','QUEUED') AND updated < ?",
                                        time.time() - RECOVERY_AGE)
                for row in rows:
                    await recover_one(row)
                if tick % 5 == 0:
                    await reconcile_failed()
            except (DBUnavailable, httpx.HTTPError) as e:
                log("RECOVERY_SKIPPED", why=type(e).__name__)
            except Exception as e:  # the worker must survive anything
                log("RECOVERY_ERROR", err=f"{type(e).__name__}: {e}")

    return app
