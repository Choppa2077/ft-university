"""Document storage for transcripts.

FT: a software RAID-1 - every document is written to two "disks" (directories)
together with its SHA-256. Reads verify the checksum; a bad or missing copy is
served from the mirror and rewritten (self-repair). This is the same idea as
ECC memory or a RAID scrub: store redundancy, detect the error, correct it.

Baseline: one disk, no checksum, whatever is on disk gets served.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class CorruptedError(Exception):
    pass


class Store:
    def __init__(self, disks: list[Path], checksums: bool, log=None):
        self.disks = disks
        self.checksums = checksums
        self.log = log or (lambda *a, **k: None)
        for d in disks:
            d.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, doc: dict) -> None:
        data = json.dumps(doc, indent=1).encode()
        digest = hashlib.sha256(data).hexdigest().encode()
        for d in self.disks:
            _atomic_write(d / f"{name}.json", data)
            if self.checksums:
                _atomic_write(d / f"{name}.sha256", digest)

    def _good_copy(self, disk: Path, name: str) -> bytes | None:
        p, s = disk / f"{name}.json", disk / f"{name}.sha256"
        if not p.exists() or not s.exists():
            return None
        data = p.read_bytes()
        return data if hashlib.sha256(data).hexdigest().encode() == s.read_bytes().strip() else None

    def read(self, name: str) -> tuple[dict | None, list[str]]:
        """Returns (document or None if never written, list of repaired disks)."""
        if not self.checksums:
            p = self.disks[0] / f"{name}.json"
            if not p.exists():
                return None, []
            return json.loads(p.read_bytes()), []

        if not any((d / f"{name}.json").exists() for d in self.disks):
            return None, []
        repaired = []
        for i, disk in enumerate(self.disks):
            data = self._good_copy(disk, name)
            if data is None:
                continue
            # repair every disk before this one that had a bad/missing copy
            for bad in self.disks[:i]:
                self.write_raw(bad, name, data)
                repaired.append(bad.name)
                self.log("STORAGE_REPAIRED", doc=name, disk=bad.name, source=disk.name)
            return json.loads(data), repaired
        self.log("STORAGE_UNRECOVERABLE", doc=name)
        raise CorruptedError(f"all copies of {name} are corrupted")

    def write_raw(self, disk: Path, name: str, data: bytes) -> None:
        _atomic_write(disk / f"{name}.json", data)
        _atomic_write(disk / f"{name}.sha256", hashlib.sha256(data).hexdigest().encode())

    def scrub(self) -> dict:
        """Check every document on every disk and fix what can be fixed."""
        names = sorted({p.stem for d in self.disks for p in d.glob("*.json")})
        stats = {"checked": 0, "repaired": 0, "unrecoverable": 0}
        for n in names:
            stats["checked"] += 1
            try:
                _, rep = self.read(n)
                if not self.checksums:
                    continue
                # read() stops at the first good copy, so also check the later disks
                good = None
                for d in self.disks:
                    good = good or self._good_copy(d, n)
                for d in self.disks:
                    if self._good_copy(d, n) is None and good is not None:
                        self.write_raw(d, n, good)
                        rep.append(d.name)
                        self.log("STORAGE_REPAIRED", doc=n, disk=d.name, source="scrub")
                stats["repaired"] += len(rep)
            except CorruptedError:
                stats["unrecoverable"] += 1
        return stats
