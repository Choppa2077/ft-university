"""Create the secrets for a local deployment (never committed: deploy/secrets/ is in .gitignore).

    python deploy/gen_secrets.py            # creates missing secrets, keeps existing ones
    python deploy/gen_secrets.py --rotate   # new values for everything

Writes:
  deploy/secrets/<name>              random tokens / keys (one file per Docker secret)
  deploy/secrets/users.json          synthetic accounts: salted PBKDF2 hashes only
  deploy/secrets/test_credentials.json  plain passwords of the synthetic accounts, read
                                     only by the load generator and the test scripts
All accounts are synthetic (S0001..S0300, registrar reg1, admin); no real user data.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ftuni.security import hash_password  # noqa: E402

DIR = Path(__file__).resolve().parent / "secrets"
TOKENS = ["jwt_key", "ops_token", "db_token_peer", "db_token_student", "db_token_payment",
          "db_token_records", "db_token_monitor", "grafana_admin"]
N_STUDENTS = 300


def write(path: Path, text: str) -> None:
    path.write_text(text)
    # readable by the unprivileged container users (Docker Desktop bind-mounts these files);
    # on a real host these would be root-owned 0400 files or a secret manager
    os.chmod(path, 0o644)


def main() -> None:
    rotate = "--rotate" in sys.argv
    DIR.mkdir(parents=True, exist_ok=True)
    for name in TOKENS:
        p = DIR / name
        if rotate or not p.exists():
            write(p, secrets.token_urlsafe(32))
    creds_path = DIR / "test_credentials.json"
    if rotate or not creds_path.exists() or not (DIR / "users.json").exists():
        creds = {f"S{i:04d}": secrets.token_urlsafe(9) for i in range(1, N_STUDENTS + 1)}
        creds["reg1"] = secrets.token_urlsafe(12)
        creds["admin"] = secrets.token_urlsafe(16)
        roles = {u: ("registrar" if u == "reg1" else "admin" if u == "admin" else "student") for u in creds}
        users = {u: {"role": roles[u], "hash": hash_password(pw)} for u, pw in creds.items()}
        write(DIR / "users.json", json.dumps(users))
        write(creds_path, json.dumps(creds))
    print(f"secrets in {DIR}: {sorted(p.name for p in DIR.iterdir())}")


if __name__ == "__main__":
    main()
