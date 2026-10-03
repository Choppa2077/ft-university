"""Student service: profile lookup and course registration."""
from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import config
from ..common import make_app
from ..dbclient import DBUnavailable


class Enroll(BaseModel):
    student_id: str
    course_id: str


def create_app(inst: config.Instance) -> FastAPI:
    app = make_app(inst, uses_db=True)

    @app.get("/students/{sid}")
    async def get_student(sid: str):
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

    @app.post("/students/register")
    async def register(req: Enroll):
        # INSERT OR IGNORE on (student, course): registering twice is harmless,
        # so the gateway and clients may retry this call safely.
        try:
            res = await app.state.db.tx(
                ("INSERT OR IGNORE INTO enrollments VALUES (?,?,?)", (req.student_id, req.course_id, time.time())))
        except DBUnavailable as e:
            raise HTTPException(503, f"database unavailable ({e})")
        return {"status": "enrolled", "new": res[0] == 1}

    return app
