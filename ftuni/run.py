"""Start one instance:  python -m ftuni.run payment-a   (supervisor locally, Docker in the endterm)."""
from __future__ import annotations

import importlib
import json
import sys
import time

import uvicorn

from . import config

MODULES = {
    "gateway": "ftuni.gateway",
    "monitor": "ftuni.monitor",
    "db": "ftuni.dbnode",
    "bank": "ftuni.services.bank",
    "student": "ftuni.services.student",
    "payment": "ftuni.services.payment",
    "records": "ftuni.services.records",
    "timetable": "ftuni.services.timetable",
}

# which secrets each kind of instance must have before it may start (endterm)
REQUIRED_SECRETS = {
    "gateway": ["jwt_key", "ops_token"],
    "db": ["db_token", "db_token_student", "db_token_payment", "db_token_records", "db_token_monitor",
           "ops_token"],
    "student": ["db_token", "ops_token"], "payment": ["db_token", "ops_token"],
    "records": ["db_token", "ops_token"], "monitor": ["db_token", "ops_token"],
    "timetable": ["ops_token"], "bank": ["ops_token"],
}


def check_secrets(kind: str) -> None:
    """Fail safe: refuse to start with a missing or placeholder secret instead of
    running with no protection."""
    pol = config.policy()
    if not (pol.auth or pol.db_acl or pol.protect_ops):
        return
    bad = [s for s in REQUIRED_SECRETS.get(kind, [])
           if len(config.secret(s)) < 24 or config.secret(s).lower().startswith("change")]
    if bad:
        print(json.dumps({"ts": time.time(), "ev": "REFUSING_TO_START", "missing_or_weak_secrets": bad}),
              flush=True)
        sys.exit(78)


def restart_delay(inst: config.Instance) -> None:
    """In Docker the container is restarted by the restart policy. A marker file tells a
    restart from the first start. The database waits a little (as db_restart_delay in
    the midterm) so that the failover decision is made before the old primary returns;
    the baseline emulates the operator who restarts things by hand after 20 s."""
    if not config.DOCKER:
        return
    marker = config.RUN_DIR / "started" / inst.name
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        pol = config.policy()
        delay = pol.db_restart_delay if inst.kind == "db" else (
            pol.restart_delay if pol.name == "baseline" else 0.0)
        print(json.dumps({"ts": time.time(), "inst": inst.name, "ev": "RESTART_DELAY", "seconds": delay}),
              flush=True)
        time.sleep(delay)
    marker.write_text(str(time.time()))


def main() -> None:
    inst = config.get(sys.argv[1])
    check_secrets(inst.kind)
    restart_delay(inst)
    app = importlib.import_module(MODULES[inst.kind]).create_app(inst)
    if config.DOCKER:
        host = "0.0.0.0"            # reachable on the container networks; only the gateway port is published
    else:
        host = config.HOST if inst.kind == "gateway" else "127.0.0.1"
    uvicorn.run(app, host=host, port=inst.port, log_level="warning", access_log=False, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
