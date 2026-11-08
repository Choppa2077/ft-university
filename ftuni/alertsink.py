"""Alertmanager webhook receiver ("the on-call engineer's pager").

Every notification is stored with its arrival time in run/alerts/alerts.jsonl, so
the experiments can measure detection delay = first FIRING notification - injection.
"""
from __future__ import annotations

import json
import time

import uvicorn
from fastapi import FastAPI, Request

from . import config


def create_app() -> FastAPI:
    out = config.RUN_DIR / "alerts"
    out.mkdir(parents=True, exist_ok=True)
    f = open(out / "alerts.jsonl", "a", buffering=1)
    app = FastAPI(title="alert-sink", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/alert")
    async def alert(request: Request):
        body = await request.json()
        now = time.time()
        for a in body.get("alerts", []):
            labels = a.get("labels", {})
            rec = {"received": round(now, 4), "status": a.get("status"), "alertname": labels.get("alertname"),
                   "severity": labels.get("severity"), "labels": labels, "startsAt": a.get("startsAt"),
                   "endsAt": a.get("endsAt"), "summary": a.get("annotations", {}).get("summary")}
            f.write(json.dumps(rec) + "\n")
            print(json.dumps({"ts": now, "inst": "alert-sink", "ev": "ALERT", **rec}), flush=True)
        return {"ok": True}

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=9095, log_level="warning")
