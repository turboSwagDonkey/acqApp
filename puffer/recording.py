"""
Standalone event log for the puffer bring-up harness (puffer/_toy.py).

NOT used by the main app — that records every stream through acq/ (Recorder +
HDF5Writer) on the shared clock. This is a minimal CSV event log so the toy can
be exercised in isolation.
"""
from __future__ import annotations
import csv
from pathlib import Path


class EventLog:
    def __init__(self) -> None:
        self._fh = None
        self._writer = None
        self._n = 0

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    def start(self, folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}_events.csv"
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["elapsed_s", "duration_s"])
        self._n = 0
        return path

    def log(self, elapsed_s: float, duration_s: float) -> None:
        if self._writer is not None:
            self._writer.writerow([f"{elapsed_s:.6f}", f"{duration_s:.6f}"])
            self._n += 1

    def stop(self) -> int:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
        return self._n
