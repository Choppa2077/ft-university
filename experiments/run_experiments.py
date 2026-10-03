"""Controlled failure experiments, baseline vs fault-tolerant.

    python -m experiments.run_experiments                 # everything, both builds
    python -m experiments.run_experiments E2 E4 --mode ft # a subset

Each run: fresh cluster -> steady load (40 req/s) -> fault at t = 10 s -> load keeps
going -> settle -> consistency check -> results/<experiment>_<mode>/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time

import httpx

from ftuni import config

from . import consistency
from .cluster import GATEWAY, Cluster
from .workload import Workload, client, save_csv

RESULTS = config.ROOT / "results"
T_INJECT = 10.0
RATE = 40


# ---------------------------------------------------------------- scenarios
# Each scenario: duration of the load, settle time, an async inject() and the rule
# that finds the "detected" moment in the event log.

async def inject_crash(cl, http, mark):
    await mark("inject", await cl.kill(http, "student-a"))


async def inject_db(cl, http, mark):
    await mark("inject", await cl.kill(http, cl.db_primary()))


async def inject_bank(cl, http, mark):
    await mark("inject", await cl.chaos(http, "bank", latency_ms=6000, latency_after=True, affect_health=True))
    await asyncio.sleep(15)
    await mark("revert", await cl.chaos(http, "bank"))


async def inject_node(cl, http, mark):
    await mark("inject", await cl.node_down(http, "A", 20))


async def inject_interrupt(cl, http, mark):
    for p in config.of_kind("payment", cl.mode):
        await cl.chaos(http, p.name, interrupt_rate=0.3, interrupt_at="after_debit")
    await mark("inject", {"interrupt_rate": 0.3, "stage": "after_debit"})
    await asyncio.sleep(15)
    for p in config.of_kind("payment", cl.mode):
        await cl.chaos(http, p.name)
    await mark("revert", {})


SCENARIOS = {
    "E1": dict(title="Application crash (student-a killed)", duration=40, settle=3, inject=inject_crash,
               detect=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "DOWN" and e["target"] == "student-a",
               restored=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "UP" and e["target"] == "student-a"),
    "E2": dict(title="Database primary failure", duration=40, settle=3, inject=inject_db,
               detect=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "DOWN" and e["target"] == "db-a",
               restored=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "UP" and e["target"] == "db-a"),
    "E3": dict(title="Slow external bank (6 s responses for 15 s)", duration=40, settle=12, inject=inject_bank,
               detect=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "DOWN" and e["target"] == "bank",
               restored=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "UP" and e["target"] == "bank"),
    "E4": dict(title="Node A failure (20 s)", duration=45, settle=3, inject=inject_node,
               detect=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "DOWN" and e.get("node") == "A",
               restored=lambda e, cl: e["inst"] == "monitor" and e["ev"] == "UP" and e.get("target") == "db-a"),
    "E5": dict(title="Interrupted payment transactions (30% for 15 s)", duration=40, settle=14,
               inject=inject_interrupt, detect=None, restored=None),
    "E6": dict(title="High load (+150 timetable req/s for 15 s)", duration=40, settle=3, inject=None,
               detect=lambda e, cl: e["inst"] == "gateway" and e["ev"] == "LOAD_SHED", restored=None),
}


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))]


def summarize(sid: str, mode: str, records, marks: dict, events: list[dict], origin: float, cl) -> dict:
    sc = SCENARIOS[sid]
    t_inj = marks.get("inject", {}).get("t", T_INJECT)
    rel = lambda e: round(e["ts"] - origin, 3)
    done = [r for r in records if r.t1 > 0]
    failed = [r for r in done if not r.ok]
    after = [r for r in failed if r.t0 >= t_inj - 0.5]
    out = {
        "experiment": sid, "mode": mode, "title": sc["title"], "t_inject": round(t_inj, 3),
        "requests": len(done), "ok": sum(r.ok for r in done), "failed": len(failed),
        "degraded": sum(r.ok and r.degraded for r in done),
        "recovered_by_retry": sum(r.ok and (r.attempts > 1 or r.try_no > 1) for r in done),
        "success_rate": round(sum(r.ok for r in done) / max(1, len(done)), 4),
        "latency_p50": round(pct([r.latency for r in done], 50), 3),
        "latency_p95": round(pct([r.latency for r in done], 95), 3),
        "latency_p99": round(pct([r.latency for r in done], 99), 3),
        "failed_by_op": {op: sum(r.op == op for r in failed) for op in sorted({r.op for r in done})},
        "timeouts": sum(r.err == "timeout" for r in done),
    }
    # detection: first matching event after the injection
    if sc["detect"]:
        det = [e for e in events if e["ts"] >= origin + t_inj - 0.05 and sc["detect"](e, cl)]
        out["detection_time"] = round(rel(det[0]) - t_inj, 3) if det else None
    if sc["restored"]:
        rst = [e for e in events if e["ts"] > origin + t_inj and sc["restored"](e, cl)]
        out["redundancy_restored_after"] = round(rel(rst[0]) - t_inj, 3) if rst else None
    # client-visible outage: from the injection to the end of the last failed request
    out["client_visible_recovery"] = round(max(r.t1 for r in after) - t_inj, 3) if after else 0.0
    out["first_failure_after"] = round(min(r.t0 for r in after) - t_inj, 3) if after else None
    # interesting log events for the report
    keys = ("DB_FAILOVER", "PROMOTED", "BREAKER", "UPSTREAM_DOWN", "UPSTREAM_UP", "RESTART", "NODE_DOWN",
            "NODE_UP", "TX_INTERRUPTED", "RECOVERED", "DUPLICATE_REQUEST", "PAYMENT_QUEUED", "COMPENSATED",
            "LOAD_SHED", "RESYNCED", "REPLICA_IN_SYNC", "FAILOVER_BLOCKED")
    out["event_counts"] = {k: sum(e["ev"] == k for e in events if e["ts"] >= origin) for k in keys}
    out["event_counts"] = {k: v for k, v in out["event_counts"].items() if v}
    if sid == "E5":
        out.update(interrupt_stats(events, origin))
    return out


def interrupt_stats(events: list[dict], origin: float) -> dict:
    """How long until each interrupted payment was picked up again and finished."""
    inter = {e["key"]: e["ts"] for e in events if e["ev"] == "TX_INTERRUPTED"}
    picked, finished = {}, {}
    for e in events:
        k = e.get("key")
        if k in inter and e["ts"] > inter[k]:
            if e["ev"] in ("DUPLICATE_REQUEST", "RECOVERED") and k not in picked:
                picked[k] = e["ts"] - inter[k]
            if e["ev"] in ("PAYMENT_COMPLETED", "RECOVERED") and k not in finished:
                finished[k] = e["ts"] - inter[k]
    return {"interrupted": len(inter), "interrupted_resolved": len(finished),
            "detection_time": round(statistics.mean(picked.values()), 3) if picked else None,
            "resolution_time_mean": round(statistics.mean(finished.values()), 3) if finished else None,
            "resolution_time_max": round(max(finished.values()), 3) if finished else None}


async def run_one(sid: str, mode: str) -> dict:
    sc = SCENARIOS[sid]
    outdir = RESULTS / f"{sid}_{mode}"
    outdir.mkdir(parents=True, exist_ok=True)
    cl = Cluster(mode)
    cl.start()
    origin = time.time() + 1.0
    marks: dict = {}

    async def mark(name: str, info: dict) -> None:
        marks[name] = {"t": round(time.time() - origin, 3), "info": info}

    try:
        main = Workload(mode, RATE, sc["duration"], origin, seed=int(sid[1:]))
        loads = [main]
        if sid == "E6":
            loads.append(Workload(mode, 150, 15, origin, mix={"timetable": 1.0}, seed=99, start_at=T_INJECT,
                                  tag="burst"))
            marks["inject"] = {"t": T_INJECT, "info": {"burst": "150 req/s timetable for 15 s"}}
        async with client() as c, httpx.AsyncClient() as http:
            tasks = [asyncio.create_task(w.run(c)) for w in loads]
            if sc["inject"]:
                await asyncio.sleep(max(0, origin + T_INJECT - time.time()))
                await sc["inject"](cl, http, mark)
            await asyncio.gather(*tasks)
        await asyncio.sleep(sc["settle"])
        records = [r for w in loads for r in w.records]
        cons = await consistency.check(mode, records)
        events = cl.events()
    finally:
        cl.stop()

    save_csv(records, outdir / "requests.csv")
    (outdir / "events.jsonl").write_text("\n".join(json.dumps({**e, "t": round(e["ts"] - origin, 3)})
                                                   for e in events) + "\n")
    summary = summarize(sid, mode, records, marks, events, origin, cl)
    summary["marks"] = marks
    summary["consistency"] = cons
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


# ------------------------------------------------ E5b: checkpointed batch job

async def run_batch(mode: str) -> dict:
    outdir = RESULTS / f"E5b_{mode}"
    outdir.mkdir(parents=True, exist_ok=True)
    cl = Cluster(mode)
    cl.start()
    term = "2025F"
    ra = cl.inst("records-a").url
    res: dict = {"experiment": "E5b", "mode": mode, "title": "Transcript batch interrupted half-way"}
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            t0 = time.time()
            await http.post(f"{ra}/transcripts/batch/{term}")
            while True:
                st = (await http.get(f"{ra}/transcripts/batch/{term}")).json()
                if st.get("next_index", 0) >= 150:
                    break
                await asyncio.sleep(0.05)
            await cl.kill(http, "records-a")
            t_kill = time.time()
            res["killed_at_index"] = st["next_index"]
            rerun_at = None
            while time.time() - t_kill < 120:
                await asyncio.sleep(0.25)
                try:
                    if mode == "ft":
                        st = (await http.get(f"{GATEWAY}/transcripts/batch/{term}")).json()
                        if st.get("done"):
                            break
                    else:
                        r = await http.get(f"{ra}/transcripts/batch/{term}")
                        if r.status_code == 404 and rerun_at is None:
                            # the operator sees the job is gone and starts it again (from zero)
                            await http.post(f"{ra}/transcripts/batch/{term}")
                            rerun_at = time.time()
                        elif r.status_code == 200 and r.json().get("next_index") == 300:
                            break
                except httpx.HTTPError:
                    pass
            t_done = time.time()
        await asyncio.sleep(1.5)
        events = cl.events()
    finally:
        cl.stop()
    work = (cl.storage() / "batch_work.log").read_text().splitlines()
    sids = [w.split(",")[1] for w in work]
    resume = [e for e in events if e["ev"] in ("BATCH_TAKEOVER",) or (e["ev"] == "BATCH_RUNNING" and e["ts"] > t_kill)]
    res.update({
        "total_time": round(t_done - t0, 2),
        "time_lost_to_failure": None,
        "work_items": len(work), "unique_transcripts": len(set(sids)), "repeated_work": len(work) - len(set(sids)),
        "resumed_from": resume[0].get("resume_from", resume[0].get("start")) if resume else None,
        "resumed_after": round(resume[0]["ts"] - t_kill, 2) if resume else None,
        "resumed_by": resume[0]["inst"] if resume else None,
        "operator_rerun_after": round(rerun_at - t_kill, 2) if rerun_at else None,
    })
    (outdir / "summary.json").write_text(json.dumps(res, indent=2))
    (outdir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return res


# ------------------------------------------------ E7: storage corruption

async def run_storage(mode: str) -> dict:
    outdir = RESULTS / f"E7_{mode}"
    outdir.mkdir(parents=True, exist_ok=True)
    cl = Cluster(mode)
    cl.start()
    res: dict = {"experiment": "E7", "mode": mode, "title": "Silent corruption of stored transcripts"}
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            ra = cl.inst("records-a").url
            await http.post(f"{ra}/transcripts/batch/2025F")
            while (await http.get(f"{ra}/transcripts/batch/2025F")).json().get("next_index", 0) < 300:
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.5)
            # ground truth straight from the database
            prim = config.get(cl.db_primary(), mode).url
            rows = await consistency._query(http, prim, (
                "SELECT g.student_id AS sid, g.course_id AS course, c.credits, g.grade FROM grades g "
                "JOIN courses c ON c.id = g.course_id ORDER BY g.student_id, g.course_id"))
            per_student: dict = {}
            for r in rows:
                per_student.setdefault(r["sid"], []).append(r)
            truth = {}          # computed exactly like records.build() does it
            for sid, rs in per_student.items():
                credits = sum(r["credits"] for r in rs)
                truth[sid] = ([{"course": r["course"], "credits": r["credits"], "grade": r["grade"]} for r in rs],
                              round(sum(r["credits"] * r["grade"] for r in rs) / credits, 2))
            # corrupt 30 files on disk1: 25 silent (a GPA digit changes, JSON stays valid), 5 garbage bytes
            rnd = random.Random(5)
            victims = rnd.sample(sorted(truth), 30)
            disk1 = cl.storage() / "disk1"
            for i, sid in enumerate(victims):
                p = disk1 / f"{sid}.json"
                data = bytearray(p.read_bytes())
                if i < 25:
                    j = data.index(b'"gpa": ') + 7
                    data[j] = ord("1") if data[j] != ord("1") else ord("2")
                else:
                    j = len(data) // 2
                    data[j:j + 4] = b"\xff\xfe\x00\x13"
                p.write_bytes(bytes(data))
            t_corrupt = time.time()
            correct = wrong = errors = repaired = 0
            wrong_examples = []
            first_detect = None
            async with client(10) as c:
                for sid in sorted(truth):
                    r = await c.get(f"/transcripts/{sid}")
                    if r.headers.get("x-repaired"):
                        repaired += 1
                        first_detect = first_detect or time.time() - t_corrupt
                    if r.status_code != 200:
                        errors += 1
                        continue
                    doc = r.json()
                    if (doc["courses"], doc["gpa"]) == truth[sid]:
                        correct += 1
                    else:
                        wrong += 1
                        if len(wrong_examples) < 3:
                            wrong_examples.append({"student": sid, "served_gpa": doc["gpa"], "true_gpa": truth[sid][1]})
            scrub = (await http.post(f"{ra}/storage/scrub")).json() if mode == "ft" else None
        events = cl.events()
    finally:
        cl.stop()
    res.update({"corrupted_files": 30, "silent": 25, "garbage": 5, "reads": len(truth), "correct": correct,
                "wrong_data_served": wrong, "errors": errors, "repaired_on_read": repaired,
                "wrong_examples": wrong_examples, "scrub_after": scrub,
                "repair_events": sum(e["ev"] == "STORAGE_REPAIRED" for e in events)})
    (outdir / "summary.json").write_text(json.dumps(res, indent=2))
    return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiments", nargs="*", default=["E1", "E2", "E3", "E4", "E5", "E5b", "E6", "E7"])
    ap.add_argument("--mode", choices=["ft", "baseline", "both"], default="both")
    args = ap.parse_args()
    modes = ["baseline", "ft"] if args.mode == "both" else [args.mode]
    for sid in args.experiments:
        for mode in modes:
            print(f"=== {sid} {mode}", flush=True)
            if sid == "E5b":
                s = await run_batch(mode)
            elif sid == "E7":
                s = await run_storage(mode)
            else:
                s = await run_one(sid, mode)
            brief = {k: s.get(k) for k in ("requests", "failed", "degraded", "detection_time",
                                            "client_visible_recovery", "total_time", "repeated_work",
                                            "wrong_data_served", "repaired_on_read") if k in s}
            if "consistency" in s:
                brief["consistent"] = s["consistency"]["consistent"]
            print(json.dumps(brief), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
