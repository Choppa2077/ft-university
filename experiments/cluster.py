"""Start/stop a cluster from Python and inject faults into it."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from ftuni import config

SUP = f"http://127.0.0.1:{config.SUPERVISOR_PORT}"
GATEWAY = f"http://127.0.0.1:{8000 + config.PORT_OFFSET}"


class Cluster:
    def __init__(self, mode: str):
        self.mode = mode
        self.proc: subprocess.Popen | None = None

    def inst(self, name: str) -> config.Instance:
        return config.get(name, self.mode)

    def start(self, timeout: float = 60.0) -> None:
        log = open(config.ROOT / "run_supervisor.log", "w")
        env = {**os.environ, "FTUNI_MODE": self.mode}
        self.proc = subprocess.Popen([sys.executable, "-m", "ftuni", "up", "--mode", self.mode],
                                     cwd=config.ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(SUP + "/ready", timeout=1).json().get("ready"):
                    time.sleep(1.0)          # let health checkers see everything once
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError("cluster did not become ready")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        from ftuni.supervisor import kill_leftovers
        kill_leftovers()
        time.sleep(0.5)

    # ---- fault injection ----
    async def kill(self, http: httpx.AsyncClient, name: str, sig: str = "KILL") -> dict:
        return (await http.post(f"{SUP}/kill/{name}?sig={sig}", timeout=2)).json()

    async def node_down(self, http: httpx.AsyncClient, node: str, seconds: float) -> dict:
        return (await http.post(f"{SUP}/node/{node}/down?seconds={seconds}", timeout=2)).json()

    async def chaos(self, http: httpx.AsyncClient, name: str, **kw) -> dict:
        return (await http.post(self.inst(name).url + "/chaos", json=kw, timeout=2)).json()

    def db_primary(self) -> str:
        return config.read_registry()["db_primary"]

    def events(self) -> list[dict]:
        out = []
        for f in (config.RUN_DIR / "logs" / "events").glob("*.jsonl"):
            for line in f.read_text().splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return sorted(out, key=lambda e: e["ts"])

    def storage(self) -> Path:
        return config.RUN_DIR / "storage"
