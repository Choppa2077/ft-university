"""Host-side control of the Docker Compose cluster and fault injection (endterm).

Fault injection never needs a door into the application: it uses the Docker API
(kill a process inside a container, pause/unpause a container) or runs a command
inside the target container (chaos switches, consistency checks).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from ftuni import config

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "run-docker"
SECRETS = ROOT / "deploy" / "secrets"
GATEWAY = "http://127.0.0.1:8000"
PROM = "http://127.0.0.1:9090"


def sh(*args: str, env: dict | None = None, timeout: float = 180, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=check)


class DockerCluster:
    def __init__(self, mode: str):
        self.mode = mode
        self.env = {**os.environ, "FTUNI_MODE": mode, "COMPOSE_PROFILES": "" if mode == "baseline" else "replicas"}
        self.ops = (SECRETS / "ops_token").read_text().strip()
        self.creds = json.loads((SECRETS / "test_credentials.json").read_text())
        self.names = [i.name for i in config.topology(mode)]

    # ---------------------------------------------------------------- lifecycle
    def down(self) -> None:
        env = {**self.env, "COMPOSE_PROFILES": "replicas"}      # also remove node B containers
        sh("docker", "compose", "down", "-v", "--remove-orphans", "-t", "1", env=env, check=False)

    def fresh(self) -> None:
        self.down()
        shutil.rmtree(RUN, ignore_errors=True)
        RUN.mkdir()

    def up(self, timeout: float = 120) -> float:
        t0 = time.time()
        r = sh("docker", "compose", "up", "-d", env=self.env, check=False)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-2000:])
        self.wait_ready(timeout)
        return time.time() - t0

    def wait_ready(self, timeout: float) -> None:
        """Ready = gateway sees every replica, Prometheus scrapes every instance, the
        monitor sees every instance ready (database replica in sync) and no alert fires."""
        deadline = time.time() + timeout
        n = len(self.names)
        last = ""
        while time.time() < deadline:
            try:
                ups = httpx.get(GATEWAY + "/upstreams", headers={"X-Ops-Token": self.ops}, timeout=2).json()
                gw_ok = all(u["healthy"] for v in ups.values() for u in v)
                up = self.prom_value(f'count(up{{job=~"ftuni|gateway"}} == 1)')
                mon = self.prom_value('count(ftuni_monitor_target_up == 1)')
                firing = self.prom('ALERTS{alertstate="firing"}')
                last = f"gw={gw_ok} up={up}/{n} monitor={mon}/{n - 1} firing={[a['metric']['alertname'] for a in firing]}"
                if gw_ok and up == n and mon == n - 1 and not firing:
                    time.sleep(1.0)
                    return
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            time.sleep(1.0)
        raise RuntimeError(f"cluster not ready: {last}")

    # ---------------------------------------------------------------- Prometheus
    def prom(self, q: str) -> list:
        return httpx.get(PROM + "/api/v1/query", params={"query": q}, timeout=5).json()["data"]["result"]

    def prom_value(self, q: str) -> float | None:
        res = self.prom(q)
        return float(res[0]["value"][1]) if res else None

    def prom_range(self, q: str, start: float, end: float, step: str = "1s") -> list:
        r = httpx.get(PROM + "/api/v1/query_range", params={"query": q, "start": start, "end": end, "step": step},
                      timeout=20)
        return r.json()["data"]["result"]

    # ---------------------------------------------------------------- fault injection
    def crash(self, name: str) -> float:
        """SIGKILL every process of the container except PID 1 (tini). The app dies, tini
        exits, and Docker's restart policy brings the container back (docker kill would
        count as a manual stop and the restart policy would not apply)."""
        t = time.time()
        sh("docker", "exec", f"ftuni-{name}", "sh", "-c", "kill -9 -1", check=False)
        return t

    def pause(self, name: str) -> float:
        t = time.time()
        sh("docker", "pause", f"ftuni-{name}")
        return t

    def unpause(self, name: str) -> float:
        t = time.time()
        sh("docker", "unpause", f"ftuni-{name}", check=False)
        return t

    def dc_network(self, name: str, net: str) -> float:
        """Disconnect a container from one network: both nodes stay alive but cannot talk
        (a real network partition), unlike a crash."""
        t = time.time()
        sh("docker", "network", "disconnect", f"ftuni_{net}", f"ftuni-{name}", check=False)
        return t

    def rc_network(self, name: str, net: str) -> float:
        t = time.time()
        sh("docker", "network", "connect", f"ftuni_{net}", f"ftuni-{name}", check=False)
        return t

    def chaos(self, name: str, **kw) -> float:
        r = sh("docker", "exec", f"ftuni-{name}", "python", "-m", "ftuni.chaosctl", name, json.dumps(kw), check=False)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])["ts"]
        except (ValueError, IndexError, KeyError):
            raise RuntimeError(f"chaos on {name} failed: {r.stdout} {r.stderr}")

    def inspect(self, name: str) -> dict:
        r = sh("docker", "inspect", "-f", "{{json .State}}|{{.RestartCount}}", f"ftuni-{name}", check=False)
        state, _, restarts = r.stdout.strip().partition("|")
        d = json.loads(state) if state else {}
        d["RestartCount"] = int(restarts or 0)
        return d

    def exec_json(self, container: str, *args: str, timeout: float = 120) -> dict:
        r = sh("docker", "exec", container, *args, timeout=timeout, check=False)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"error": (r.stdout + r.stderr)[-1500:]}

    # ---------------------------------------------------------------- evidence
    def db_primary(self) -> str:
        return json.loads((RUN / "registry.json").read_text())["db_primary"]

    def events(self, since: float = 0.0) -> list[dict]:
        out = []
        for f in (RUN / "logs" / "events").glob("*.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", 0) >= since:
                    out.append(e)
        return sorted(out, key=lambda e: e["ts"])

    def alerts(self) -> list[dict]:
        p = RUN / "alerts" / "alerts.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []

    def audit_records(self) -> list[dict]:
        out = []
        for f in (RUN / "audit").glob("*.jsonl"):
            out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return sorted(out, key=lambda r: r["ts"])

    def audit_verify(self) -> dict:
        return self.exec_json("ftuni-monitor", "python", "-m", "ftuni.audit", "verify", "/app/run/audit")

    def consistency(self, records) -> dict:
        acked = [{"op": r.op, "status": r.status, "ok": r.ok, "intent": r.intent, "detail": r.detail}
                 for r in records]
        (RUN / "acked.json").write_text(json.dumps(acked))
        return self.exec_json("ftuni-monitor", "python", "-m", "experiments.consistency_cli", "check")

    def checksum(self) -> dict:
        return self.exec_json("ftuni-monitor", "python", "-m", "experiments.consistency_cli", "checksum")
