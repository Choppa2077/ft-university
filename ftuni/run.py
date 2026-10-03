"""Start one instance:  python -m ftuni.run payment-a   (normally done by the supervisor)."""
from __future__ import annotations

import importlib
import sys

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


def main() -> None:
    inst = config.get(sys.argv[1])
    app = importlib.import_module(MODULES[inst.kind]).create_app(inst)
    host = config.HOST if inst.kind == "gateway" else "127.0.0.1"
    uvicorn.run(app, host=host, port=inst.port, log_level="warning", access_log=False, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
