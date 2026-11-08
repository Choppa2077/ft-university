"""Security verification: X5 (unauthorized access) and X6 (abusive / malformed requests).

Every case is a probe against the running cluster that states the expected HTTP
outcome; the experiment passes only if the observed outcome matches AND the business
data is byte-identical before and after (a database checksum is taken around the run).
The point is to show the controls hold, not to break anything: all data is synthetic.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ftuni import config


async def _login(c: httpx.AsyncClient, user: str, creds: dict) -> str | None:
    r = await c.post("/auth/login", json={"username": user, "password": creds[user]})
    return r.json()["access_token"] if r.status_code == 200 else None


def _bearer(tok: str | None) -> dict:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


async def unauthorized_cases(c: httpx.AsyncClient, creds: dict, cluster) -> list[dict]:
    """X5: each probe = (name, expected status set, description). A protected action with
    no/der wrong authority must be refused; a forged or expired token must be rejected."""
    student = await _login(c, "S0001", creds)
    victim_hdr = _bearer(student)
    reg = await _login(c, "reg1", creds)
    forged = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
              "eyJzdWIiOiJTMDAwMSIsInJvbGUiOiJhZG1pbiIsImlhdCI6MCwiZXhwIjo5OTk5OTk5OTk5LCJpc3MiOiJmdHVuaS1nYXRld2F5In0."
              "deadbeef")   # admin claim signed with the wrong key
    none_alg = ("eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
                "eyJzdWIiOiJTMDAwMSIsInJvbGUiOiJhZG1pbiIsImlhdCI6MCwiZXhwIjo5OTk5OTk5OTk5LCJpc3MiOiJmdHVuaS1nYXRld2F5In0.")
    cases: list[tuple[str, str, str, dict, set, str]] = [
        ("no_token", "GET", "/students/S0001", {}, {401}, "no credentials"),
        ("forged_admin_token", "GET", "/students/S0002", _bearer(forged), {401},
         "token with admin claim signed with the wrong key"),
        ("alg_none_token", "GET", "/students/S0002", _bearer(none_alg), {401}, "unsigned 'alg:none' token"),
        ("idor_read_other_student", "GET", "/students/S0002", victim_hdr, {403}, "read another student's profile"),
        ("idor_read_other_transcript", "GET", "/transcripts/S0002", victim_hdr, {403},
         "read another student's transcript"),
        ("student_changes_grade", "POST", "/transcripts/grades", victim_hdr, {403, 422},
         "student tries to change a grade (registrar-only)"),
        ("pay_for_other_student", "POST", "/payments", victim_hdr, {403, 422},
         "pay on another student's behalf"),
        ("student_runs_batch", "POST", "/transcripts/batch/2025F", victim_hdr, {403},
         "student starts the transcript batch (registrar-only)"),
        ("header_spoof_identity", "GET", "/students/S0002",
         {**victim_hdr, "X-User-Id": "S0002", "X-User-Role": "admin"}, {403},
         "spoof X-User-Id/Role headers to impersonate the victim"),
        ("ops_endpoint_as_user", "POST", "/chaos", victim_hdr, {403, 404},
         "trigger the fault-injection API as a normal user"),
        ("read_metrics_no_ops_token", "GET", "/metrics", {}, {403}, "scrape the gateway metrics without the ops token"),
    ]
    bodies = {"/transcripts/grades": {"student_id": "S0002", "course_id": "C001", "grade": 4.0},
              "/payments": {"student_id": "S0002", "amount": 100000}}
    results = []
    for name, method, path, hdr, expect, desc in cases:
        h = dict(hdr)
        if path == "/payments":
            h["Idempotency-Key"] = "atk-" + name[:20]
        r = await c.request(method, path, json=bodies.get(path), headers=h)
        results.append({"case": name, "layer": "gateway", "description": desc, "method": method, "path": path,
                        "status": r.status_code, "expected": sorted(expect), "ok": r.status_code in expect})
    return results


def insider_cases(cluster) -> list[dict]:
    """X5, insider: run from inside the compromised student-a container. The database ACL
    must deny writes/snapshots/promotion even with the student service's own credential."""
    probes = [
        ("sql_no_token", {401}, "direct SQL to the DB with no credential"),
        ("sql_stolen_token", {403}, "UPDATE grades with the student service's stolen DB token"),
        ("read_payments_stolen_token", {400, 403}, "read the payments table with the stolen token"),
        ("drop_table_stolen_token", {403}, "DROP TABLE with the stolen token"),
        ("promote_replica", {401, 403}, "force a replica to promote (split brain) with the stolen token"),
        ("snapshot_dump", {403}, "dump a full database snapshot with the stolen token"),
    ]
    out = []
    for case, expect, desc in probes:
        res = cluster.exec_json("ftuni-student-a", "python", "-m", "experiments.insider", case)
        out.append({"case": case, "layer": "database", "description": desc,
                    "status": res.get("status"), "expected": sorted(expect),
                    "ok": res.get("status") in expect, "detail": res.get("body", res.get("error", ""))[:120]})
    return out


async def malformed_cases(c: httpx.AsyncClient, creds: dict) -> list[dict]:
    """X6: malformed / hostile input must be refused (4xx), never crash or corrupt data."""
    tok = await _login(c, "S0001", creds)
    H = _bearer(tok)
    big = "A" * (64 * 1024)
    cases = [
        ("negative_amount", "POST", "/payments", {"student_id": "S0001", "amount": -100000}, {400, 422}),
        ("zero_amount", "POST", "/payments", {"student_id": "S0001", "amount": 0}, {400, 422}),
        ("overflow_amount", "POST", "/payments", {"student_id": "S0001", "amount": 10 ** 15}, {400, 422}),
        ("amount_as_string", "POST", "/payments", {"student_id": "S0001", "amount": "120000"}, {400, 422}),
        ("sql_in_id", "GET", "/students/S0001' OR '1'='1", None, {403, 404, 422}),
        ("sql_in_course", "POST", "/students/register",
         {"student_id": "S0001", "course_id": "C001); DROP TABLE grades;--"}, {400, 404, 422}),
        ("unknown_field", "POST", "/payments", {"student_id": "S0001", "amount": 1000, "role": "admin"}, {400, 422}),
        ("bad_group", "GET", "/timetable/'; DROP TABLE students;--", None, {403, 404, 422}),
        ("oversize_body", "POST", "/payments", {"student_id": "S0001", "amount": 1000, "note": big}, {413, 422}),
        ("not_json", "POST", "/payments", "not json at all", {400, 422}),
    ]
    out = []
    for name, method, path, body, expect in cases:
        h = dict(H)
        if path == "/payments":
            h["Idempotency-Key"] = "mal-" + name[:20]
        kw = {"content": body} if isinstance(body, str) else {"json": body}
        r = await c.request(method, path, headers=h, **kw)
        out.append({"case": name, "description": f"{method} {path[:40]}", "status": r.status_code,
                    "expected": sorted(expect), "ok": r.status_code in expect})
    return out


async def abuse_flood(c: httpx.AsyncClient, creds: dict, n: int = 400) -> dict:
    """X6, abuse: one authenticated student fires a burst; the rate limiter must throttle
    it (429) while staying up. Returns the status histogram."""
    tok = await _login(c, "S0002", creds)
    H = _bearer(tok)
    sem = asyncio.Semaphore(50)
    counts = {"200": 0, "429": 0, "other": 0}

    async def one(i: int):
        async with sem:
            try:
                r = await c.get("/students/S0002", headers=H)
                key = "200" if r.status_code == 200 else "429" if r.status_code == 429 else "other"
                counts[key] += 1
            except httpx.HTTPError:
                counts["other"] += 1
    t0 = time.time()
    await asyncio.gather(*(one(i) for i in range(n)))
    return {"sent": n, "seconds": round(time.time() - t0, 2), **counts,
            "throttled": counts["429"] > 0}
