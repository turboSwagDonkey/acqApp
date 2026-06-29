"""
Puffer — event log.  Records each puff as a timestamped row in a CSV.
No Qt dependency.
"""

from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path


class EventLog:

    def __init__(self) -> None:
        self._fh     = None
        self._writer = None
        self._count  = 0
        self._path:  Path | None = None

    def start(self, folder: Path, stem: str) -> Path:
        if self._fh is not None:
            raise RuntimeError("Already open")
        folder.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{stem}_puffer_{ts}.csv"
        self._fh     = open(path, "w", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["event_idx", "timestamp_s", "duration_s"])
        self._path   = path
        self._count  = 0
        return path

    def log(self, timestamp_s: float, duration_s: float) -> None:
        if self._writer is None:
            return
        self._count += 1
        self._writer.writerow([self._count, f"{timestamp_s:.6f}", f"{duration_s:.3f}"])

    def stop(self) -> int:
        if self._fh:
            self._fh.flush(); self._fh.close()
            self._fh = self._writer = None
        return self._count

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    @property
    def event_count(self) -> int:
        return self._count

    @property
    def path(self) -> Path | None:
        return self._path
