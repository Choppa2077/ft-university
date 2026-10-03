"""Open-loop load generator.

Requests are fired on a fixed schedule whether or not the previous ones have
finished, like real users would. Every request is recorded (start, end, status,
which replica served it, how many gateway attempts it took, degraded or not).

Client behaviour is the same in both builds: a student whose payment fails or
times out presses "Pay" again once, after 1 s. The only difference is that the
FT client sends an Idempotency-Key (the same key on the retry).
"""
from __future__ import annotations

import asyncio
import csv
import random
import time
import uuid
from dataclasses import asdict, dataclass, fields

import httpx

MIX = {"student": 0.35, "enroll": 0.15, "pay": 0.20, "transcript": 0.15, "timetable": 0.15}
GROUPS = [f"CS-25{n:02d}" for n in range(1, 11)]


@dataclass
class Rec:
    op: str
    t0: float
    t1: float = 0.0
    status: int = 0
    ok: bool = False
    degraded: bool = False
    served_by: str = ""
    attempts: int = 1
    err: str = ""
    intent: str = ""
    try_no: int = 1
    detail: str = ""

    @property
    def latency(self) -> float:
        return self.t1 - self.t0


class Workload:
    def __init__(self, mode: str, rate: float, duration: float, origin: float, mix: dict | None = None,
                 seed: int = 1, start_at: float = 0.0, client_retry: bool = True, tag: str = "main"):
        self.mode = mode
        self.rate = rate
        self.duration = duration
        self.origin = origin            # wall-clock time that counts as t = 0
        self.mix = mix or MIX
        self.seed = seed
        self.start_at = start_at
        self.client_retry = client_retry
        self.tag = tag
        self.records: list[Rec] = []

    async def run(self, client: httpx.AsyncClient) -> None:
        rnd = random.Random(self.seed)
        ops, weights = zip(*self.mix.items())
        tasks = []
        n = 0
        while True:
            t = self.start_at + n / self.rate
            if t >= self.start_at + self.duration:
                break
            delay = self.origin + t - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            op = rnd.choices(ops, weights)[0]
            args = (f"S{rnd.randint(1, 300):04d}", f"C{rnd.randint(1, 40):03d}",
                    rnd.choice(GROUPS), rnd.randint(1, 4), rnd.choice([50000, 120000, 250000, 480000]))
            tasks.append(asyncio.create_task(self.do(client, op, *args)))
            n += 1
        await asyncio.gather(*tasks)

    async def do(self, c: httpx.AsyncClient, op: str, sid: str, course: str, group: str, week: int,
                 amount: int) -> None:
        if op == "pay":
            await self.pay(c, sid, amount)
            return
        headers = {"Idempotency-Key": uuid.uuid4().hex} if self.mode == "ft" else {}
        if op == "student":
            await self.send(c, op, "GET", f"/students/{sid}")
        elif op == "enroll":
            rec = await self.send(c, op, "POST", "/students/register",
                                  {"student_id": sid, "course_id": course}, headers)
            rec.detail = f"{sid}:{course}"
        elif op == "transcript":
            await self.send(c, op, "GET", f"/transcripts/{sid}")
        elif op == "timetable":
            await self.send(c, op, "GET", f"/timetable/{group}?week={week}")

    async def pay(self, c: httpx.AsyncClient, sid: str, amount: int) -> None:
        intent = uuid.uuid4().hex[:16]
        for try_no in (1, 2):
            headers = {"X-Client-Intent": intent}
            if self.mode == "ft":
                headers["Idempotency-Key"] = intent
            rec = await self.send(c, "pay", "POST", "/payments", {"student_id": sid, "amount": amount}, headers,
                                  intent=intent, try_no=try_no)
            if rec.ok or not self.client_retry:
                return
            await asyncio.sleep(1.0)

    async def send(self, c: httpx.AsyncClient, op: str, method: str, path: str, body: dict | None = None,
                   headers: dict | None = None, intent: str = "", try_no: int = 1) -> Rec:
        rec = Rec(op, round(time.time() - self.origin, 4), intent=intent, try_no=try_no)
        self.records.append(rec)
        try:
            r = await c.request(method, path, json=body, headers=headers)
            rec.status = r.status_code
            rec.ok = 200 <= r.status_code < 300
            rec.degraded = r.status_code == 202 or "x-degraded" in r.headers
            rec.served_by = r.headers.get("x-served-by", "")
            rec.attempts = int(r.headers.get("x-attempts", "1"))
            if op == "pay":
                try:
                    rec.detail = str(r.json().get("status", ""))
                except ValueError:
                    pass
        except httpx.TimeoutException:
            rec.err = "timeout"
        except httpx.HTTPError as e:
            rec.err = type(e).__name__
        rec.t1 = round(time.time() - self.origin, 4)
        return rec


def save_csv(records: list[Rec], path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([fl.name for fl in fields(Rec)])
        for r in records:
            w.writerow(list(asdict(r).values()))


def load_csv(path) -> list[Rec]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(Rec(op=row["op"], t0=float(row["t0"]), t1=float(row["t1"]), status=int(row["status"]),
                           ok=row["ok"] == "True", degraded=row["degraded"] == "True", served_by=row["served_by"],
                           attempts=int(row["attempts"]), err=row["err"], intent=row["intent"],
                           try_no=int(row["try_no"]), detail=row["detail"]))
    return out


def client(timeout: float = 5.0) -> httpx.AsyncClient:
    from .cluster import GATEWAY
    return httpx.AsyncClient(base_url=GATEWAY, timeout=timeout,
                             limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
