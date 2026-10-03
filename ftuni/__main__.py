"""
python -m ftuni up --mode ft          start the fault-tolerant build (Ctrl+C stops everything)
python -m ftuni up --mode baseline    start the baseline build
python -m ftuni status                show processes and what the monitor sees
python -m ftuni down                  kill everything left from a previous run
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from . import config


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m ftuni", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--mode", choices=["ft", "baseline"], default="ft")
    up.add_argument("--keep-data", action="store_true", help="do not wipe run/ before starting")
    sub.add_parser("status")
    sub.add_parser("down")
    args = ap.parse_args()

    if args.cmd == "up":
        from .supervisor import run
        print(f"starting the {args.mode} build, gateway on http://127.0.0.1:{8000 + config.PORT_OFFSET}")
        run(args.mode, fresh=not args.keep_data)
    elif args.cmd == "down":
        from .supervisor import kill_leftovers
        kill_leftovers()
        print("done")
    elif args.cmd == "status":
        try:
            procs = httpx.get(f"http://127.0.0.1:{config.SUPERVISOR_PORT}/procs", timeout=2).json()
        except httpx.HTTPError:
            sys.exit("supervisor is not running")
        try:
            mon = httpx.get(config.get("monitor", "ft").url + "/status", timeout=2).json()
        except httpx.HTTPError:
            mon = {}
        for name, p in procs.items():
            seen = mon.get(name, {})
            role = seen.get("info", {}).get("role", "")
            print(f"{name:12} node {p['node']:4} alive={str(p['alive']):5} restarts={p['restarts']} "
                  f"monitor={'UP' if seen.get('up') else 'DOWN' if seen else '?':4} {role}")
        print(json.dumps(config.read_registry()))


if __name__ == "__main__":
    main()
