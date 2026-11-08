"""Student service: profile lookup and course registration."""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import config
from ..common import identity, make_app
from ..dbclient import DBUnavailable

SID = r"^S\d{4}$"
CID = r"^C\d{3}$"


class Enroll(BaseModel):                 # midterm builds: anything goes
    student_id: str
    course_id: str


class EnrollStrict(BaseModel):           # endterm: exact formats, no unknown fields
    model_config = ConfigDict(extra="forbid")
    student_id: str = Field(pattern=SID)
    course_id: str = Field(pattern=CID)


def create_app(inst: config.Instance) -> FastAPI:
    P = config.policy()
    app = make_app(inst, uses_db=True)

    def owner_only(request: Request, sid: str) -> None:
        # defence in depth: the gateway already checked this
        uid, role = identity(request)
        if P.auth and role == "student" and uid != sid:
            app.state.audit("AUTHZ_DENIED", "denied", service="student", reason="not_owner", target=sid)
            raise HTTPException(403, "forbidden")

    async def read_student(sid: str, request: Request):
        owner_only(request, sid)
        try:
            rows = await app.state.db.query(
                "SELECT s.id, s.name, s.grp, "
                "(SELECT GROUP_CONCAT(course_id) FROM enrollments e WHERE e.student_id = s.id) AS enrolled "
                "FROM students s WHERE s.id = ?", sid)
        except DBUnavailable as e:
            raise HTTPException(503, f"database unavailable ({e})")
        if not rows:
            raise HTTPException(404, "no such student")
        row = rows[0]
        row["enrolled"] = sorted(row["enrolled"].split(",")) if row["enrolled"] else []
        return row

    async def do_register(req, request: Request):
        owner_only(request, req.student_id)
        # INSERT OR IGNORE on (student, course): registering twice is harmless,
        # so the gateway and clients may retry this call safely.
        try:
            if P.validation:
                ex = (await app.state.db.query(
                    "SELECT (SELECT COUNT(*) FROM students WHERE id=?) AS s, "
                    "(SELECT COUNT(*) FROM courses WHERE id=?) AS c", req.student_id, req.course_id))[0]
                if not (ex["s"] and ex["c"]):
                    raise HTTPException(404, "unknown student or course")
            res = await app.state.db.tx(
                ("INSERT OR IGNORE INTO enrollments VALUES (?,?,?)", (req.student_id, req.course_id, time.time())))
        except DBUnavailable as e:
            raise HTTPException(503, f"database unavailable ({e})")
        return {"status": "enrolled", "new": res[0] == 1}

    if P.validation:
        @app.get("/students/{sid}")
        async def get_student(sid: Annotated[str, Path(pattern=SID)], request: Request):
            return await read_student(sid, request)

        @app.post("/students/register")
        async def register(req: EnrollStrict, request: Request):
            return await do_register(req, request)
    else:
        @app.get("/students/{sid}")
        async def get_student_lax(sid: str, request: Request):
            return await read_student(sid, request)

        @app.post("/students/register")
        async def register_lax(req: Enroll, request: Request):
            return await do_register(req, request)

    return app
