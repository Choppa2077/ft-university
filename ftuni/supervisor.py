"""Process supervisor - plays the role Kubernetes/systemd would play.

Starts every instance of the chosen build as a separate OS process, restarts
processes that die (FT: after 1 s, database after 3 s; baseline: after 20 s, our
stand-in for an operator noticing and restarting it by hand), and exposes a small
control API used by the fault-injection scripts:

  POST /kill/{name}?sig=KILL|STOP|CONT     crash / freeze / unfreeze one process
  POST /node/{node}/down?seconds=20       "power off" a node: kill everything on it
  GET  /procs                              what is running
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException

from . import config
from .common import EventLog

START_ORDER = ["bank", "db", "student", "payment", "records", "timetable", "gateway", "monitor"]


class Proc:
    def __init__(self, inst: config.Instance):
        self.inst = inst
        self.popen: subprocess.Popen | None = None
        self.restarts = 0
        self.next_start: float | None = None
        self.exited_at: float | None = None


class Supervisor:
    def __init__(self, mode: str):
        self.mode = mode
        self.policy = config.policy(mode)
        self.procs = {i.name: Proc(i) for i in config.topology(mode)}
        self.node_down_until: dict[str, float] = {}
        self.ready = False
        self.log = EventLog("supervisor")

    # -- process handling --
    def spawn(self, name: str) -> None:
        pr = self.procs[name]
        out = open(config.RUN_DIR / "logs" / f"{name}.out", "ab")
        env = {**os.environ, "FTUNI_MODE": self.mode, "FTUNI_RUN_DIR": str(config.RUN_DIR),
               "PYTHONUNBUFFERED": "1"}
        pr.popen = subprocess.Popen([sys.executable, "-m", "ftuni.run", name], cwd=config.ROOT, env=env,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        self.save_pids()
        self.log("SPAWN", name=name, pid=pr.popen.pid)

    def save_pids(self) -> None:
        pids = {n: p.popen.pid for n, p in self.procs.items() if p.popen and p.popen.poll() is None}
        (config.RUN_DIR / "pids.json").write_text(json.dumps({"supervisor": os.getpid(), **pids}))

    def restart_delay(self, pr: Proc) -> float:
        return self.policy.db_restart_delay if pr.inst.kind == "db" else self.policy.restart_delay

    async def wait_ready(self, names: list[str], timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        async with httpx.AsyncClient() as http:
            pending = set(names)
            while pending and time.time() < deadline:
                for n in list(pending):
                    try:
                        r = await http.get(self.procs[n].inst.url + "/health/ready", timeout=0.5)
                        if r.status_code == 200:
                            pending.discard(n)
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(0.2)
        return not pending

    async def start_all(self) -> None:
        for kind in START_ORDER:
            names = [n for n, p in self.procs.items() if p.inst.kind == kind]
            for n in names:
                self.spawn(n)
                if kind == "db":    # primary first, the replica copies it on start
                    await self.wait_ready([n])
            if kind in ("bank", "db"):
                await self.wait_ready(names)
        self.ready = await self.wait_ready(list(self.procs))
        self.log("CLUSTER_READY" if self.ready else "CLUSTER_NOT_READY", mode=self.mode)

    async def watch(self) -> None:
        while True:
            now = time.time()
            for node, until in list(self.node_down_until.items()):
                if now >= until:
                    del self.node_down_until[node]
                    self.log("NODE_UP", node=node)
                    for pr in self.procs.values():
                        if pr.inst.node == node and pr.popen is None:
                            pr.next_start = now          # the node boots, its services start
            for name, pr in self.procs.items():
                if pr.popen is not None and pr.popen.poll() is not None:
                    self.log("EXITED", name=name, code=pr.popen.returncode)
                    pr.popen = None
                    pr.exited_at = now
                    pr.next_start = now + self.restart_delay(pr)
                    self.save_pids()
                node_down = pr.inst.node in self.node_down_until
                if pr.popen is None and pr.next_start is not None and now >= pr.next_start and not node_down:
                    pr.restarts += 1
                    pr.next_start = None
                    self.log("RESTART", name=name, down_for=round(now - (pr.exited_at or now), 2))
                    self.spawn(name)
            await asyncio.sleep(0.1)

    def signal_proc(self, name: str, sig: signal.Signals) -> int:
        pr = self.procs.get(name)
        if pr is None or pr.popen is None or pr.popen.poll() is not None:
            raise HTTPException(409, f"{name} is not running")
        os.kill(pr.popen.pid, sig)
        self.log("SIGNAL", name=name, sig=sig.name, pid=pr.popen.pid)
        return pr.popen.pid

    def node_down(self, node: str, seconds: float) -> list[str]:
        self.node_down_until[node] = time.time() + seconds
        killed = []
        for name, pr in self.procs.items():
            if pr.inst.node == node and pr.popen is not None and pr.popen.poll() is None:
                os.kill(pr.popen.pid, signal.SIGKILL)
                killed.append(name)
        self.log("NODE_DOWN", node=node, seconds=seconds, killed=killed)
        return killed

    def stop_all(self) -> None:
        for pr in self.procs.values():
            if pr.popen and pr.popen.poll() is None:
                try:
                    os.kill(pr.popen.pid, signal.SIGCONT)
                    pr.popen.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.time() + 3
        for pr in self.procs.values():
            if pr.popen:
                try:
                    pr.popen.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    pr.popen.kill()
        self.log("STOPPED")


def kill_leftovers() -> None:
    """Kill processes from a previous run that was not shut down cleanly."""
    f = config.RUN_DIR / "pids.json"
    if not f.exists():
        return
    try:
        pids = json.loads(f.read_text())
    except ValueError:
        return
    for name, pid in pids.items():
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    f.unlink()


def prepare_run_dir(fresh: bool) -> None:
    kill_leftovers()
    if fresh and config.RUN_DIR.exists():
        shutil.rmtree(config.RUN_DIR)
    for sub in ("logs/events", "data", "storage"):
        (config.RUN_DIR / sub).mkdir(parents=True, exist_ok=True)
    if fresh or not config.REGISTRY.exists():
        config.write_registry({"db_primary": "db-a", "epoch": 1})


def create_app(sup: Supervisor) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = asyncio.create_task(sup.watch())
        starter = asyncio.create_task(sup.start_all())
        yield
        starter.cancel()
        watcher.cancel()
        sup.stop_all()

    app = FastAPI(title="supervisor", lifespan=lifespan)

    @app.get("/ready")
    async def ready():
        return {"ready": sup.ready, "mode": sup.mode}

    @app.get("/procs")
    async def procs():
        return {n: {"node": p.inst.node, "port": p.inst.port, "pid": p.popen.pid if p.popen else None,
                    "alive": bool(p.popen and p.popen.poll() is None), "restarts": p.restarts}
                for n, p in sup.procs.items()}

    @app.post("/kill/{name}")
    async def kill(name: str, sig: str = "KILL"):
        return {"name": name, "pid": sup.signal_proc(name, getattr(signal, "SIG" + sig.upper()))}

    @app.post("/node/{node}/down")
    async def node_down(node: str, seconds: float = 20.0):
        return {"node": node, "killed": sup.node_down(node, seconds)}

    return app


def run(mode: str, fresh: bool = True) -> None:
    prepare_run_dir(fresh)
    os.environ["FTUNI_MODE"] = mode
    sup = Supervisor(mode)
    uvicorn.run(create_app(sup), host="127.0.0.1", port=config.SUPERVISOR_PORT, log_level="warning")
