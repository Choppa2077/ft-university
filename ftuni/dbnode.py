"""Database node: SQLite behind a small HTTP API, with primary -> replica log shipping.

Writes go to the primary. Each write transaction gets a log sequence number (LSN)
and is stored in the _log table together with the SQL it ran. The primary ships
every entry to the replica *before* committing locally, so an acknowledged write
always exists on both nodes (synchronous replication). If the replica is down the
primary keeps going on its own and a background loop ships the backlog later.

Failover is driven by the monitor: it bumps the epoch in run/registry.json and
calls /promote on the replica. The epoch works as a fencing token - a node with
an old epoch can no longer accept writes or replicate into a newer node.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from . import config
from .common import make_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS students(id TEXT PRIMARY KEY, name TEXT, grp TEXT);
CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY, title TEXT, credits INTEGER);
CREATE TABLE IF NOT EXISTS grades(student_id TEXT, course_id TEXT, term TEXT, grade REAL,
                                  PRIMARY KEY(student_id, course_id, term));
CREATE TABLE IF NOT EXISTS enrollments(student_id TEXT, course_id TEXT, ts REAL,
                                       PRIMARY KEY(student_id, course_id));
CREATE TABLE IF NOT EXISTS payments(key TEXT PRIMARY KEY, student_id TEXT, amount INTEGER,
                                    status TEXT, bank_ref TEXT, intent TEXT, note TEXT,
                                    created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS ledger(payment_key TEXT PRIMARY KEY, student_id TEXT,
                                  amount INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS batch_jobs(term TEXT PRIMARY KEY, total INTEGER, next_index INTEGER,
                                      owner TEXT, lease_until REAL, done INTEGER,
                                      started REAL, finished REAL);
CREATE TABLE IF NOT EXISTS _log(lsn INTEGER PRIMARY KEY, epoch INTEGER, tx_id TEXT UNIQUE,
                                ops TEXT, result TEXT, ts REAL);
"""

N_STUDENTS = 300
GROUPS = [f"CS-25{n:02d}" for n in range(1, 11)]


def seed(conn: sqlite3.Connection) -> None:
    rnd = random.Random(42)
    courses = [(f"C{c:03d}", f"Course {c}", rnd.choice([3, 4, 5, 6])) for c in range(1, 41)]
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO courses VALUES (?,?,?)", courses)
    for s in range(1, N_STUDENTS + 1):
        sid = f"S{s:04d}"
        conn.execute("INSERT INTO students VALUES (?,?,?)", (sid, f"Student {s}", GROUPS[(s - 1) % 10]))
        for cid, _, _ in rnd.sample(courses, 12):
            grade = rnd.choice([4.0, 3.67, 3.33, 3.0, 2.67, 2.33, 2.0, 1.67, 1.0])
            conn.execute("INSERT INTO grades VALUES (?,?,?,?)", (sid, cid, "2025F", grade))
    conn.execute("COMMIT")


class Op(BaseModel):
    sql: str
    params: list = []


class TxReq(BaseModel):
    tx_id: str
    ops: list[Op]
    min_epoch: int = 0


class QueryReq(BaseModel):
    sql: str
    params: list = []
    allow_stale: bool = False


class ReplReq(BaseModel):
    epoch: int
    entries: list[dict]


class PromoteReq(BaseModel):
    epoch: int


class Node:
    def __init__(self, inst: config.Instance):
        self.inst = inst
        self.dir = config.RUN_DIR / "data" / inst.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "uni.db"
        peers = [i for i in config.of_kind("db") if i.name != inst.name]
        self.peer = peers[0] if peers else None
        self.role = "replica"
        self.epoch = 0
        self.lsn = 0
        self.synced = False          # replica holds a full copy and follows the primary
        self.peer_state = "absent"   # primary's view of the replica: sync | lagging | absent
        self.lock = asyncio.Lock()
        self.w: sqlite3.Connection | None = None   # write connection
        self.r: sqlite3.Connection | None = None   # read connection (sees committed data only)
        self.resync_task: asyncio.Task | None = None

    def open(self) -> None:
        self.w = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.w.execute("PRAGMA journal_mode=WAL")
        self.w.execute("PRAGMA synchronous=NORMAL")
        self.w.executescript(SCHEMA)
        self.r = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.r.execute("PRAGMA query_only=ON")
        self.lsn = self.w.execute("SELECT COALESCE(MAX(lsn), 0) FROM _log").fetchone()[0]

    def close(self) -> None:
        for c in (self.w, self.r):
            if c is not None:
                c.close()
        self.w = self.r = None

    def entries_after(self, lsn: int, limit: int = 500) -> list[dict]:
        rows = self.r.execute("SELECT lsn, tx_id, ops, result FROM _log WHERE lsn > ? ORDER BY lsn LIMIT ?",
                              (lsn, limit)).fetchall()
        return [{"lsn": a, "tx_id": b, "ops": json.loads(c), "result": json.loads(d)} for a, b, c, d in rows]

    def info(self) -> dict:
        return {"role": self.role, "epoch": self.epoch, "lsn": self.lsn,
                "synced": self.synced, "peer_state": self.peer_state}


def create_app(inst: config.Instance) -> FastAPI:
    node = Node(inst)
    holder: dict = {}

    def ready():
        ok = node.w is not None and (node.role == "primary" or node.synced)
        return ok, node.info()

    async def startup(app: FastAPI):
        holder["http"] = app.state.http
        log = app.state.log
        reg = config.read_registry()
        node.epoch = reg["epoch"]
        if reg["db_primary"] == inst.name:
            node.open()
            peer = await peek()
            if peer and peer["role"] == "primary" and peer["epoch"] >= node.epoch:
                # someone took over while we were down - come back as a replica
                log("STARTUP_DEPOSED", peer_epoch=peer["epoch"])
                step_down()
            else:
                node.role = "primary"
                if node.w.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
                    seed(node.w)
                    log("SEEDED", students=N_STUDENTS)
                if peer and peer["role"] == "replica" and peer["lsn"] > node.lsn and peer["epoch"] == node.epoch:
                    await pull_from_peer(peer["lsn"])
                log("ROLE", role="primary", epoch=node.epoch, lsn=node.lsn)
        else:
            step_down()
        return [asyncio.create_task(catchup_loop())] if node.peer else []

    app = make_app(inst, startup=startup, ready=ready)
    log = app.state.log

    async def peek() -> dict | None:
        if not node.peer:
            return None
        try:
            return (await holder["http"].get(node.peer.url + "/role", timeout=0.5)).json()
        except (httpx.HTTPError, ValueError):
            return None

    def step_down() -> None:
        node.role = "replica"
        node.synced = False
        node.peer_state = "absent"
        if node.resync_task is None or node.resync_task.done():
            node.resync_task = asyncio.create_task(resync())

    async def resync() -> None:
        """Replace the local database with a fresh snapshot of the primary."""
        while True:
            try:
                prim = config.get(config.read_registry()["db_primary"])
                if prim.name != inst.name:
                    r = await holder["http"].get(prim.url + "/snapshot", timeout=10.0)
                    if r.status_code == 200:
                        async with node.lock:
                            node.close()
                            for suffix in ("", "-wal", "-shm"):
                                p = node.dir / ("uni.db" + suffix)
                                if p.exists():
                                    p.unlink()
                            tmp = node.dir / "incoming.db"
                            tmp.write_bytes(r.content)
                            os.replace(tmp, node.path)
                            node.open()
                            node.epoch = int(r.headers["x-epoch"])
                            node.role = "replica"
                            node.synced = True
                        log("RESYNCED", source=prim.name, lsn=node.lsn, epoch=node.epoch)
                        return
            except (httpx.HTTPError, KeyError, OSError):
                pass
            await asyncio.sleep(0.5)

    async def pull_from_peer(peer_lsn: int) -> None:
        # We crashed after shipping an entry but before committing it ourselves.
        r = await holder["http"].get(node.peer.url + f"/log?after={node.lsn}", timeout=2.0)
        for e in r.json()["entries"]:
            apply_entry(e, node.epoch)
        log("PULLED_FROM_REPLICA", lsn=node.lsn)

    def apply_entry(e: dict, epoch: int) -> None:
        node.w.execute("BEGIN IMMEDIATE")
        try:
            for o in e["ops"]:
                node.w.execute(o["sql"], o["params"])
            node.w.execute("INSERT INTO _log VALUES (?,?,?,?,?,?)",
                           (e["lsn"], epoch, e["tx_id"], json.dumps(e["ops"]), json.dumps(e["result"]), time.time()))
        except sqlite3.Error:
            node.w.execute("ROLLBACK")
            raise
        node.w.execute("COMMIT")
        node.lsn = e["lsn"]

    async def ship(entries: list[dict]) -> tuple[bool, dict]:
        try:
            r = await holder["http"].post(node.peer.url + "/replicate",
                                          json={"epoch": node.epoch, "entries": entries}, timeout=0.5)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            return r.status_code == 200, body
        except (httpx.HTTPError, ValueError):
            return False, {}

    async def catchup_loop() -> None:
        """Primary side: bring a lagging or returning replica back in sync."""
        while True:
            await asyncio.sleep(0.3)
            if node.role != "primary" or node.peer_state == "sync":
                continue
            peer = await peek()
            if peer is None:
                node.peer_state = "absent"
                continue
            if peer["role"] == "primary":
                if peer["epoch"] > node.epoch:
                    log("DEPOSED", peer_epoch=peer["epoch"])
                    step_down()
                continue
            if not peer["synced"]:
                continue
            entries = node.entries_after(peer["lsn"])
            if entries:
                ok, body = await ship(entries)
                if not ok:
                    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
                    if detail.get("error") in ("diverged", "gap"):
                        try:
                            await holder["http"].post(node.peer.url + "/resync", timeout=1.0)
                        except httpx.HTTPError:
                            pass
                    continue
            async with node.lock:
                peer = await peek()
                if peer and peer["synced"] and peer["lsn"] == node.lsn:
                    node.peer_state = "sync"
                    log("REPLICA_IN_SYNC", lsn=node.lsn)

    @app.get("/role")
    async def role():
        return {"inst": inst.name, **node.info()}

    @app.post("/tx")
    async def tx(req: TxReq):
        if node.role != "primary" or node.w is None:
            raise HTTPException(409, {"error": "not_primary", **node.info()})
        if req.min_epoch > node.epoch:
            raise HTTPException(409, {"error": "stale_epoch", **node.info()})
        async with node.lock:
            if node.role != "primary":
                raise HTTPException(409, {"error": "not_primary", **node.info()})
            dup = node.w.execute("SELECT lsn, result FROM _log WHERE tx_id=?", (req.tx_id,)).fetchone()
            if dup:  # the client retried a write we already applied
                return {"lsn": dup[0], "result": json.loads(dup[1]), "duplicate": True}
            lsn = node.lsn + 1
            ops = [o.model_dump() for o in req.ops]
            node.w.execute("BEGIN IMMEDIATE")
            try:
                result = [node.w.execute(o["sql"], o["params"]).rowcount for o in ops]
                node.w.execute("INSERT INTO _log VALUES (?,?,?,?,?,?)",
                               (lsn, node.epoch, req.tx_id, json.dumps(ops), json.dumps(result), time.time()))
            except sqlite3.Error as e:
                node.w.execute("ROLLBACK")
                raise HTTPException(400, {"error": "sql", "detail": str(e)})
            if node.peer and node.peer_state == "sync":
                ok, body = await ship([{"lsn": lsn, "tx_id": req.tx_id, "ops": ops, "result": result}])
                err = body.get("detail", {}).get("error") if isinstance(body.get("detail"), dict) else None
                if not ok and err in ("stale_epoch", "i_am_primary"):
                    # the other node has been promoted: we are fenced off, do not commit
                    node.w.execute("ROLLBACK")
                    log("FENCED", epoch=node.epoch, peer_error=err)
                    step_down()
                    raise HTTPException(409, {"error": "not_primary", **node.info()})
                if not ok:
                    node.peer_state = "lagging"
                    log("REPLICA_LAGGING", lsn=lsn, reason=err or "unreachable")
            node.w.execute("COMMIT")
            node.lsn = lsn
        return {"lsn": lsn, "result": result}

    @app.post("/query")
    async def query(req: QueryReq):
        if node.r is None:
            raise HTTPException(503, {"error": "syncing"})
        if node.role != "primary" and not req.allow_stale:
            raise HTTPException(409, {"error": "not_primary", **node.info()})
        cur = node.r.execute(req.sql, req.params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return {"cols": cols, "rows": cur.fetchall()}

    @app.post("/replicate")
    async def replicate(req: ReplReq):
        if req.epoch < node.epoch:
            raise HTTPException(409, {"error": "stale_epoch", **node.info()})
        if node.role == "primary":
            if req.epoch > node.epoch:
                step_down()
            raise HTTPException(409, {"error": "i_am_primary", **node.info()})
        if not node.synced:
            raise HTTPException(409, {"error": "syncing", **node.info()})
        node.epoch = req.epoch
        async with node.lock:
            for e in req.entries:
                if e["lsn"] <= node.lsn:
                    row = node.w.execute("SELECT tx_id FROM _log WHERE lsn=?", (e["lsn"],)).fetchone()
                    if row and row[0] != e["tx_id"]:
                        raise HTTPException(409, {"error": "diverged", **node.info()})
                    continue
                if e["lsn"] != node.lsn + 1:
                    raise HTTPException(409, {"error": "gap", **node.info()})
                try:
                    apply_entry(e, req.epoch)
                except sqlite3.Error:
                    raise HTTPException(409, {"error": "diverged", **node.info()})
        return {"lsn": node.lsn}

    @app.get("/log")
    async def get_log(after: int = 0):
        return {"entries": node.entries_after(after, limit=100000)}

    @app.get("/snapshot")
    async def snapshot():
        if node.role != "primary":
            raise HTTPException(409, {"error": "not_primary", **node.info()})
        async with node.lock:
            tmp = node.dir / "snapshot.db"
            if tmp.exists():
                tmp.unlink()
            dst = sqlite3.connect(tmp)
            node.w.backup(dst)
            dst.close()
            data, lsn, epoch = tmp.read_bytes(), node.lsn, node.epoch
        log("SNAPSHOT_SERVED", lsn=lsn, bytes=len(data))
        return Response(data, media_type="application/octet-stream",
                        headers={"x-lsn": str(lsn), "x-epoch": str(epoch)})

    @app.post("/promote")
    async def promote(req: PromoteReq):
        async with node.lock:
            if node.role == "primary" and node.epoch >= req.epoch:
                return node.info()
            if not node.synced:
                raise HTTPException(409, {"error": "not_in_sync", **node.info()})
            node.role = "primary"
            node.epoch = req.epoch
            node.peer_state = "absent"
        log("PROMOTED", epoch=req.epoch, lsn=node.lsn)
        return node.info()

    @app.post("/demote")
    async def demote():
        if node.role == "primary":
            log("DEMOTED", epoch=node.epoch)
            step_down()
        return node.info()

    @app.post("/resync")
    async def force_resync():
        if node.role == "replica":
            node.synced = False
            if node.resync_task is None or node.resync_task.done():
                node.resync_task = asyncio.create_task(resync())
        return node.info()

    return app
