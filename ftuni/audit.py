"""Append-only, hash-chained audit log (tamper-evident).

Every security-relevant decision (login, refused request, rate limiting, grade
change, payment, operator action) is written as one JSON line to
run/audit/<instance>.jsonl. Each record carries the SHA-256 of the previous record,
so editing, deleting or reordering any line breaks the chain from that point on:

    python -m ftuni.audit verify [directory]

Limitation: someone who can rewrite the *whole* file can also recompute the chain.
Production systems anchor the head hash somewhere else (a WORM bucket, a ledger);
here the head is exposed as a metric and in /audit/head.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

from . import config, obs

GENESIS = "0" * 64


def _digest(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, inst_name: str, enabled: bool = True, directory: Path | None = None):
        self.inst = inst_name
        self.enabled = enabled
        self.seq = 0
        self.prev = GENESIS
        if not enabled:
            return
        d = directory or (config.RUN_DIR / "audit")
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{inst_name}.jsonl"
        if self.path.exists():                 # restarted container: continue the chain
            for line in self.path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    self.seq, self.prev = rec["seq"], rec["hash"]
                except (ValueError, KeyError):
                    continue
        self.f = open(self.path, "a", buffering=1)

    def __call__(self, event: str, outcome: str, **details) -> None:
        if not self.enabled:
            return
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 4), "inst": self.inst, "event": event,
               "outcome": outcome, "actor": obs.actor.get() or "anonymous",
               "request_id": obs.request_id.get(), **details, "prev": self.prev}
        rec["hash"] = _digest(rec)
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.prev = rec["hash"]
        obs.AUDIT_RECORDS.labels(event, outcome).inc()

    def head(self) -> dict:
        return {"inst": self.inst, "seq": self.seq, "hash": self.prev}


def verify_file(path: Path) -> dict:
    prev, n = GENESIS, 0
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return {"file": path.name, "ok": False, "records": n, "line": lineno, "reason": "not JSON"}
        if rec.get("prev") != prev:
            return {"file": path.name, "ok": False, "records": n, "line": lineno, "reason": "broken link"}
        if _digest(rec) != rec.get("hash"):
            return {"file": path.name, "ok": False, "records": n, "line": lineno, "reason": "hash mismatch"}
        prev, n = rec["hash"], n + 1
    return {"file": path.name, "ok": True, "records": n, "head": prev}


def verify_dir(directory: Path) -> dict:
    files = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    results = [verify_file(f) for f in files]
    return {"ok": all(r["ok"] for r in results), "files": results,
            "records": sum(r["records"] for r in results)}


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "verify":
        target = Path(sys.argv[2]) if len(sys.argv) > 2 else config.RUN_DIR / "audit"
        res = verify_dir(target)
        print(json.dumps(res, indent=1))
        sys.exit(0 if res["ok"] else 1)
    print("usage: python -m ftuni.audit verify [directory]")
