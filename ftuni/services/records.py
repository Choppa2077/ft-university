"""Academic records service: transcripts and the end-of-term transcript batch job."""
from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import config
from ..common import identity, make_app
from ..dbclient import DBUnavailable
from ..storage import CorruptedError, Store

SID = r"^S\d{4}$"


class GradeChange(BaseModel):
    """Registrar corrects one grade (an academic-record operation)."""
    model_config = ConfigDict(extra="forbid")
    student_id: str = Field(pattern=SID)
    course_id: str = Field(pattern=r"^C\d{3}$")
    term: str = Field(default="2025F", pattern=r"^\d{4}[FS]$")
    grade: float = Field(ge=0.0, le=4.0)

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

    def owner_only(request: Request, sid: str) -> None:
        uid, role = identity(request)
        if P.auth and role == "student" and uid != sid:     # defence in depth (gateway checks first)
            app.state.audit("AUTHZ_DENIED", "denied", service="records", reason="not_owner", target=sid)
            raise HTTPException(403, "forbidden")

    async def transcript(sid: str, request: Request, response: Response):
        owner_only(request, sid)
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

    if P.validation:
        @app.get("/transcripts/{sid}")
        async def get_transcript(sid: Annotated[str, Path(pattern=SID)], request: Request, response: Response):
            return await transcript(sid, request, response)
    else:
        @app.get("/transcripts/{sid}")
        async def get_transcript_lax(sid: str, request: Request, response: Response):
            return await transcript(sid, request, response)

    @app.post("/transcripts/grades")
    async def change_grade(req: GradeChange, request: Request):
        uid, role = identity(request)
        if P.auth and role not in ("registrar", "admin"):
            app.state.audit("AUTHZ_DENIED", "denied", service="records", reason="registrar_only")
            raise HTTPException(403, "forbidden")
        try:
            old = await db().query("SELECT grade FROM grades WHERE student_id=? AND course_id=? AND term=?",
                                   req.student_id, req.course_id, req.term)
            if not old:
                raise HTTPException(404, "no such grade")
            await db().tx(("UPDATE grades SET grade=? WHERE student_id=? AND course_id=? AND term=?",
                           (req.grade, req.student_id, req.course_id, req.term)))
        except DBUnavailable as e:
            raise HTTPException(503, f"database unavailable ({e})")
        store.delete(req.student_id)      # the stored transcript is stale now; rebuilt on next read
        app.state.audit("GRADE_CHANGED", "allowed", student_id=req.student_id, course_id=req.course_id,
                        term=req.term, old=old[0]["grade"], new=req.grade)
        log("GRADE_CHANGED", student_id=req.student_id, course_id=req.course_id, old=old[0]["grade"], new=req.grade)
        return {"status": "updated", "student_id": req.student_id, "course_id": req.course_id,
                "old": old[0]["grade"], "new": req.grade}

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
