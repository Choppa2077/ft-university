"""Run the consistency checks from inside the cluster (the database is on an internal
network). The host-side runner calls:

    docker exec ftuni-monitor python -m experiments.consistency_cli check   # reads run/acked.json
    docker exec ftuni-monitor python -m experiments.consistency_cli checksum
"""
from __future__ import annotations

import asyncio
import json
import sys

from ftuni import config

from . import consistency
from .workload import Rec


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "checksum":
        print(json.dumps(asyncio.run(consistency.checksum(config.MODE))))
        return
    acked = json.loads(sys.stdin.read() or "[]")
    records = [Rec(op=a["op"], t0=0, status=a["status"], ok=a["ok"], intent=a["intent"], detail=a["detail"])
               for a in acked]
    print(json.dumps(asyncio.run(consistency.check(config.MODE, records))))


if __name__ == "__main__":
    main()
