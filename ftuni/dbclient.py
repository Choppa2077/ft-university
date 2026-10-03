"""Database client used by the services.

Baseline: talks to the one database node, single attempt.
FT: on a timeout, connection error or "not primary" answer it asks both nodes who
the primary is (highest epoch wins) and retries with backoff until a deadline.
Retrying a write is safe because every transaction carries a tx_id and the node
ignores a tx_id it has already applied.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from . import config
from .resilience import backoff_delay


class DBUnavailable(Exception):
    pass


class DBError(Exception):
    pass


class DB:
    def __init__(self, http: httpx.AsyncClient, policy: config.Policy, log):
        self.http = http
        self.p = policy
        self.log = log
        self.nodes = [i.url for i in config.of_kind("db")]
        reg = config.read_registry()
        self.primary = config.get(reg["db_primary"]).url
        self.epoch = reg["epoch"]

    async def find_primary(self) -> None:
        best = None
        for url in self.nodes:
            try:
                d = (await self.http.get(url + "/role", timeout=0.3)).json()
            except (httpx.HTTPError, ValueError):
                continue
            if d.get("role") == "primary" and (best is None or d["epoch"] > best[1]):
                best = (url, d["epoch"])
        if best:
            if best[0] != self.primary:
                self.log("DB_SWITCHED", primary=best[0], epoch=best[1])
            self.primary, self.epoch = best

    async def _call(self, path: str, payload: dict) -> dict:
        if not self.p.db_failover:
            try:
                r = await self.http.post(self.primary + path, json=payload, timeout=self.p.db_timeout)
            except httpx.HTTPError as e:
                raise DBUnavailable(type(e).__name__)
            if r.status_code == 400:
                raise DBError(r.text)
            if r.status_code != 200:
                raise DBUnavailable(f"http {r.status_code}")
            return r.json()

        start = time.monotonic()
        attempt = 0
        while True:
            payload["min_epoch"] = self.epoch
            try:
                r = await self.http.post(self.primary + path, json=payload, timeout=self.p.db_timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 400:
                    raise DBError(r.text)
                why = f"http {r.status_code}"
            except httpx.HTTPError as e:
                why = type(e).__name__
            if time.monotonic() - start > self.p.db_deadline:
                raise DBUnavailable(why)
            await asyncio.sleep(backoff_delay(attempt, 0.05, 0.4))
            attempt += 1
            await self.find_primary()

    async def tx(self, *ops: tuple, tx_id: str | None = None) -> list[int]:
        """Run statements atomically on the primary. Returns the rowcount of each one."""
        payload = {"tx_id": tx_id or uuid.uuid4().hex,
                   "ops": [{"sql": sql, "params": list(params)} for sql, params in ops]}
        return (await self._call("/tx", payload))["result"]

    async def query(self, sql: str, *params) -> list[dict]:
        d = await self._call("/query", {"sql": sql, "params": list(params)})
        return [dict(zip(d["cols"], row)) for row in d["rows"]]
