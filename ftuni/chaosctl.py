"""Set fault-injection switches of the instance this container runs.

    docker exec ftuni-bank python -m ftuni.chaosctl bank '{"latency_ms": 6000, "latency_after": true}'
    docker exec ftuni-bank python -m ftuni.chaosctl bank '{}'          # back to normal

Runs inside the container, so the chaos API never has to be reachable from outside;
in the endterm build it also needs the operator token (mounted as a Docker secret).
"""
from __future__ import annotations

import json
import sys
import time

import httpx

from . import config


def main() -> None:
    inst = config.get(sys.argv[1])
    body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    headers = {"X-Ops-Token": config.secret("ops_token")}
    r = httpx.post(f"http://127.0.0.1:{inst.port}/chaos", json=body, headers=headers, timeout=5)
    print(json.dumps({"ts": time.time(), "inst": inst.name, "status": r.status_code, "chaos": r.json()}))
    sys.exit(0 if r.status_code == 200 else 1)


if __name__ == "__main__":
    main()
