"""External bank / payment provider simulator.

Debits are idempotent by key (like Stripe/Kaspi style idempotency keys): sending the
same key twice returns the first debit instead of charging again. Whether the caller
actually reuses keys is up to the caller - the baseline payment service does not.
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import config
from ..common import make_app


class Debit(BaseModel):
    key: str
    student_id: str
    amount: int
    intent: str = ""     # client-side trace id, stored only so experiments can find double charges


def create_app(inst: config.Instance) -> FastAPI:
    d = config.RUN_DIR / "data" / "bank"
    d.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(d / "bank.db", isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS debits(key TEXT PRIMARY KEY, intent TEXT, student_id TEXT, "
               "amount INTEGER, ts REAL, refunded INTEGER DEFAULT 0)")

    app = make_app(inst)

    def row(key: str):
        r = db.execute("SELECT rowid, key, intent, student_id, amount, ts, refunded FROM debits WHERE key=?",
                       (key,)).fetchone()
        if r is None:
            return None
        return {"ref": f"BNK-{r[0]:06d}", "key": r[1], "intent": r[2], "student_id": r[3],
                "amount": r[4], "ts": r[5], "refunded": bool(r[6])}

    @app.post("/debit")
    async def debit(req: Debit):
        cur = db.execute("INSERT OR IGNORE INTO debits(key, intent, student_id, amount, ts) VALUES (?,?,?,?,?)",
                         (req.key, req.intent, req.student_id, req.amount, time.time()))
        return {**row(req.key), "replay": cur.rowcount == 0}

    @app.get("/debit/{key}")
    async def get_debit(key: str):
        r = row(key)
        if r is None:
            raise HTTPException(404, "no such debit")
        return r

    @app.post("/refund/{key}")
    async def refund(key: str):
        db.execute("UPDATE debits SET refunded=1 WHERE key=?", (key,))
        r = row(key)
        if r is None:
            raise HTTPException(404, "no such debit")
        return r

    @app.get("/debits")
    async def all_debits():
        rows = db.execute("SELECT key, intent, student_id, amount, ts, refunded FROM debits").fetchall()
        return [{"key": a, "intent": b, "student_id": c, "amount": e, "ts": f, "refunded": bool(g)}
                for a, b, c, e, f, g in rows]

    return app
