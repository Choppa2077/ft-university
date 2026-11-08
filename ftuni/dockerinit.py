"""One-shot init container: prepares the shared run directory for a fresh cluster.

* creates the directory layout used by every container
* writes the database registry (db-a is the first primary, epoch 1)
* writes the Prometheus scrape targets for the chosen build (file-based service
  discovery), so the baseline is not alerted about replicas it does not have
"""
from __future__ import annotations

import json
import os

from . import config


def main() -> None:
    run = config.RUN_DIR
    for sub in ("logs/events", "data", "storage", "audit", "alerts", "started", "prometheus"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    if not config.REGISTRY.exists():
        config.write_registry({"db_primary": "db-a", "epoch": 1})
    targets = [{"targets": [f"{i.name}:{i.port}"], "labels": {"name": i.name, "kind": i.kind, "node": i.node}}
               for i in config.topology() if i.kind != "gateway"]
    (run / "prometheus" / "services.json").write_text(json.dumps(targets, indent=1))
    if hasattr(os, "geteuid") and os.geteuid() == 0:      # running as root only for this step
        for dirpath, dirnames, filenames in os.walk(run):
            for n in [dirpath] + [os.path.join(dirpath, f) for f in filenames]:
                os.chown(n, 10001, 10001)
    print(json.dumps({"ev": "INIT_DONE", "mode": config.MODE, "targets": len(targets)}), flush=True)


if __name__ == "__main__":
    main()
