"""Academic records service: transcripts and the end-of-term transcript batch job."""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, HTTPException, Response

from .. import config
from ..common import make_app
from ..dbclient import DBUnavailable
from ..storage import CorruptedError, Store

LEASE = 3.0               # a batch owner must renew its lease at least this often
CHECKPOINT_EVERY = 10     # transcripts between two checkpoints
ITEM_COST = 0.02          # stand-in for rendering one transcript PDF


def create_app(inst: config.Instance) -> FastAPI:
    P = config.policy()
    root = config.RUN_DIR / "storage"
    disks = [root / "disk1"] + ([root / "disk2"] if P.mirror_storage else [])
    work_log = root / "batch_work.log"
    progress: dict[str, int] = {}     # baseline keeps batch progress only in memory

    async def startup(app: FastAPI):
        return [asyncio.create_task(takeover_loop())] if P.checkpointing else []

    app = make_app(inst, startup=startup, uses_db=True)
    log = app.state.log
    store = Store(disks, checksums=P.mirror_storage, log=log)

    def db():
        return app.state.db

    async def build(sid: str) -> dict:
        rows = await db().query(
            "SELECT s.name, s.grp, g.course_id, c.credits, g.grade FROM students s "
            "JOIN grades g ON g.student_id = s.id JOIN courses c ON c.id = g.course_id "
            "WHERE s.id = ? ORDER BY g.course_id", sid)
        if not rows:
            raise HTTPException(404, "no such student")
        credits = sum(r["credits"] for r in rows)
        gpa = round(sum(r["credits"] * r["grade"] for r in rows) / credits, 2)
        return {"student_id": sid, "name": rows[0]["name"], "group": rows[0]["grp"], "term": "2025F",
                "courses": [{"course": r["course_id"], "credits": r["credits"], "grade": r["grade"]} for r in rows],
                "total_credits": credits, "gpa": gpa}

    @app.get("/transcripts/{sid}")
    async def transcript(sid: str, response: Response):
        try:
            doc, repaired = store.read(sid)
        except CorruptedError:
            doc, repaired = None, []          # both copies bad: rebuild from the database
        except ValueError:
            raise HTTPException(500, "stored transcript is unreadable")
        if repaired:
            response.headers["X-Repaired"] = ",".join(repaired)
        if doc is None:
            try:
                doc = await build(sid)
            except DBUnavailable as e:
                raise HTTPException(503, f"database unavailable ({e})")
            store.write(sid, doc)
        return doc

    @app.post("/storage/scrub")
    async def scrub():
        return store.scrub()

    # ---------------- batch job with checkpoints ----------------

    async def run_batch(term: str, start: int) -> None:
        try:
            students = [r["id"] for r in await db().query("SELECT id FROM students ORDER BY id")]
            i = start
            log("BATCH_RUNNING", term=term, start=i, total=len(students))
            while i < len(students):
                sid = students[i]
                doc = await build(sid)
                await asyncio.sleep(ITEM_COST)
                store.write(sid, doc)
                with open(work_log, "a") as f:
                    f.write(f"{term},{sid},{inst.name},{time.time():.3f}\n")
                i += 1
                progress[term] = i
                if P.checkpointing and (i % CHECKPOINT_EVERY == 0 or i == len(students)):
                    res = await db().tx(("UPDATE batch_jobs SET next_index=?, lease_until=? WHERE term=? AND owner=?",
                                         (i, time.time() + LEASE, term, inst.name)))
                    if res[0] == 0:
                        log("BATCH_LEASE_LOST", term=term, at=i)
                        return
            if P.checkpointing:
                await db().tx(("UPDATE batch_jobs SET done=1, finished=? WHERE term=? AND owner=?",
                               (time.time(), term, inst.name)))
            log("BATCH_DONE", term=term, total=len(students))
        except (DBUnavailable, HTTPException) as e:
            # FT: the lease runs out and someone resumes from the last checkpoint
            log("BATCH_ABORTED", term=term, at=progress.get(term), why=str(e))

    @app.post("/transcripts/batch/{term}", status_code=202)
    async def start_batch(term: str):
        now = time.time()
        if P.checkpointing:
            rows = await db().query("SELECT * FROM batch_jobs WHERE term=?", term)
            if rows and not rows[0]["done"] and rows[0]["lease_until"] > now:
                return {"status": "already running", "owner": rows[0]["owner"]}
            total = (await db().query("SELECT COUNT(*) AS n FROM students"))[0]["n"]
            await db().tx(("INSERT OR REPLACE INTO batch_jobs VALUES (?,?,?,?,?,?,?,?)",
                           (term, total, 0, inst.name, now + LEASE, 0, now, None)))
        progress[term] = 0
        asyncio.create_task(run_batch(term, 0))
        return {"status": "started", "owner": inst.name}

    @app.get("/transcripts/batch/{term}")
    async def batch_status(term: str):
        if P.checkpointing:
            rows = await db().query("SELECT * FROM batch_jobs WHERE term=?", term)
            if rows:
                return rows[0]
        if term in progress:
            return {"term": term, "next_index": progress[term], "owner": inst.name}
        raise HTTPException(404, "unknown batch")

    async def takeover_loop() -> None:
        """Resume any batch whose owner stopped renewing its lease (crashed or hung)."""
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.time()
                rows = await db().query("SELECT term, next_index FROM batch_jobs WHERE done=0 AND lease_until < ?", now)
                for r in rows:
                    res = await db().tx(("UPDATE batch_jobs SET owner=?, lease_until=? "
                                         "WHERE term=? AND done=0 AND lease_until < ?",
                                         (inst.name, time.time() + LEASE, r["term"], now)))
                    if res[0] == 1:
                        log("BATCH_TAKEOVER", term=r["term"], resume_from=r["next_index"])
                        asyncio.create_task(run_batch(r["term"], r["next_index"]))
            except (DBUnavailable, HTTPException):
                pass

    return app
