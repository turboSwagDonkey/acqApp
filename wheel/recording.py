"""
Wheel encoder — CSV recording manager.  No Qt dependency.

Writes three columns: time_s, voltage_V, derived (revs or m/s depending on
whether volts_per_rev was set).  Call write() on every sample while recording.
"""

from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path


class CSVWriter:

    def __init__(self) -> None:
        self._fh      = None
        self._writer  = None
        self._path:   Path | None = None
        self._count   = 0

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self, folder: Path, stem: str,
              volts_per_rev: float | None = None) -> Path:
        if self._fh is not None:
            raise RuntimeError("Already recording")

        folder.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{stem}_encoder_{ts}.csv"

        self._fh     = open(path, "w", newline="")
        self._writer = csv.writer(self._fh)
        derived_col  = "revs" if volts_per_rev else "voltage_raw_V"
        self._writer.writerow(["time_s", "voltage_V", derived_col])
        self._path   = path
        self._count  = 0
        return path

    def write(self, time_s: float, voltage: float,
              derived: float | None = None) -> None:
        if self._writer is None:
            return
        self._writer.writerow([f"{time_s:.6f}", f"{voltage:.6f}",
                                f"{derived:.6f}" if derived is not None else ""])
        self._count += 1

    def stop(self) -> int:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = self._writer = None
        n = self._count
        self._count = 0
        return n

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._fh is not None

    @property
    def sample_count(self) -> int:
        return self._count

    @property
    def path(self) -> Path | None:
        return self._path
