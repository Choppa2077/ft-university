"""Timetable service: builds a weekly timetable for a group.

The generator is deliberately CPU-heavy (a small local search, roughly 25 ms per
request) so that the high-load experiment actually saturates it.
"""
from __future__ import annotations

import random

from fastapi import FastAPI

from .. import config
from ..common import make_app

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
HOURS = ["08:00", "09:50", "11:40", "13:30", "15:20", "17:10"]
ITERATIONS = 2000


def solve(group: str, week: int) -> dict:
    rnd = random.Random(f"{group}-{week}")
    courses = [f"C{c:03d}" for c in rnd.sample(range(1, 41), 8)]
    teacher = {c: f"T{rnd.randint(1, 5)}" for c in courses}
    sessions = [c for c in courses for _ in range(2)]          # two classes per course a week
    slots = [(d, h) for d in range(len(DAYS)) for h in range(len(HOURS))]
    plan = [rnd.choice(slots) for _ in sessions]

    def cost(p):
        c = 0
        seen_slot, seen_teacher, seen_day = {}, {}, {}
        for course, (d, h) in zip(sessions, p):
            c += 5 * seen_slot.get((d, h), 0)                    # group can't be in two places
            c += 5 * seen_teacher.get((teacher[course], d, h), 0)
            c += 2 * seen_day.get((course, d), 0)                # same course twice a day
            c += 1 if h == len(HOURS) - 1 else 0                  # nobody likes the last slot
            seen_slot[(d, h)] = seen_slot.get((d, h), 0) + 1
            seen_teacher[(teacher[course], d, h)] = seen_teacher.get((teacher[course], d, h), 0) + 1
            seen_day[(course, d)] = seen_day.get((course, d), 0) + 1
        return c

    best = cost(plan)
    for _ in range(ITERATIONS):
        i = rnd.randrange(len(plan))
        old = plan[i]
        plan[i] = rnd.choice(slots)
        new = cost(plan)
        if new <= best:
            best = new
        else:
            plan[i] = old
    lessons = sorted(zip(plan, sessions))
    return {"group": group, "week": week, "penalty": best,
            "lessons": [{"day": DAYS[d], "time": HOURS[h], "course": c, "teacher": teacher[c]}
                        for (d, h), c in lessons]}


def create_app(inst: config.Instance) -> FastAPI:
    app = make_app(inst)

    @app.get("/timetable/{group}")
    def timetable(group: str, week: int = 1):   # plain def -> runs in the thread pool
        return solve(group, week)

    return app
