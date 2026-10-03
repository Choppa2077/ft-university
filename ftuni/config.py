"""Cluster layout plus every setting that differs between the baseline and the FT build.

Both builds run the same code. The mode only decides how many replicas exist and
which protection mechanisms are switched on (see BASELINE and FT below).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = Path(os.environ.get("FTUNI_RUN_DIR", ROOT / "run")).resolve()
MODE = os.environ.get("FTUNI_MODE", "ft")
HOST = os.environ.get("FTUNI_HOST", "127.0.0.1")
PORT_OFFSET = int(os.environ.get("FTUNI_PORT_OFFSET", "0"))
SUPERVISOR_PORT = 8009 + PORT_OFFSET


@dataclass(frozen=True)
class Instance:
    name: str   # e.g. "payment-b"
    kind: str   # gateway | monitor | bank | db | student | payment | records | timetable
    node: str   # "A", "B" = application nodes, "ctl" = control node, "ext" = external bank
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def topology(mode: str = MODE) -> list[Instance]:
    o = PORT_OFFSET
    out = [Instance("gateway", "gateway", "ctl", 8000 + o),
           Instance("monitor", "monitor", "ctl", 8001 + o),
           Instance("bank", "bank", "ext", 8002 + o)]
    # the baseline has one copy of everything, all on node A
    nodes = ["A", "B"] if mode == "ft" else ["A"]
    for i, node in enumerate(nodes):
        base = 8100 + 100 * i + o
        s = node.lower()
        out += [Instance(f"db-{s}", "db", node, base + 10),
                Instance(f"student-{s}", "student", node, base + 20),
                Instance(f"payment-{s}", "payment", node, base + 30),
                Instance(f"records-{s}", "records", node, base + 40),
                Instance(f"timetable-{s}", "timetable", node, base + 50)]
    return out


def get(name: str, mode: str = MODE) -> Instance:
    return next(i for i in topology(mode) if i.name == name)


def of_kind(kind: str, mode: str = MODE) -> list[Instance]:
    return [i for i in topology(mode) if i.kind == kind]


# --- who is the database primary right now (written by the monitor on failover) ---

REGISTRY = RUN_DIR / "registry.json"


def read_registry() -> dict:
    return json.loads(REGISTRY.read_text())


def write_registry(data: dict) -> None:
    tmp = REGISTRY.with_name("registry.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, REGISTRY)


# --- the two builds ---

@dataclass(frozen=True)
class Policy:
    name: str
    # gateway
    gw_timeout: dict = field(default_factory=dict)  # seconds per upstream service
    gw_retries: int = 0
    health_checks: bool = False
    breaker: bool = False
    load_shedding: bool = False
    degrade: bool = False
    # database client
    db_failover: bool = False
    db_timeout: float = 10.0
    db_deadline: float = 0.0
    # payment service
    idempotency: bool = False
    bank_timeout: float = 30.0
    bank_retries: int = 0
    recovery_worker: bool = False
    # records service
    mirror_storage: bool = False
    checkpointing: bool = False
    # supervisor: how long until a crashed process is started again
    restart_delay: float = 20.0
    db_restart_delay: float = 20.0

    def timeout(self, kind: str) -> float:
        return self.gw_timeout.get(kind, self.gw_timeout.get("*", 30.0))


# Baseline: one instance of everything, generous timeouts, no retries, no idempotency.
# A crashed process is restarted by "the operator" after 20 s (compressed manual repair).
BASELINE = Policy(name="baseline", gw_timeout={"*": 30.0})

FT = Policy(
    name="ft",
    gw_timeout={"student": 2.5, "payment": 4.5, "records": 3.0, "timetable": 1.5},
    gw_retries=2,
    health_checks=True,
    breaker=True,
    load_shedding=True,
    degrade=True,
    db_failover=True,
    db_timeout=1.0,
    db_deadline=4.0,
    idempotency=True,
    bank_timeout=0.8,
    bank_retries=2,
    recovery_worker=True,
    mirror_storage=True,
    checkpointing=True,
    restart_delay=1.0,
    db_restart_delay=3.0,
)


def policy(mode: str | None = None) -> Policy:
    return FT if (mode or MODE) == "ft" else BASELINE
