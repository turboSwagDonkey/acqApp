"""
Pupil camera — recording managers.

FrameWriter  : saves raw frames as BigTIFF (same as voltage_cam).
TrackingLog  : writes PupilResult rows to CSV alongside the frames.
"""

from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path

import numpy as np


class FrameWriter:
    """Identical interface to voltage_cam.recording.RecordingManager."""

    def __init__(self) -> None:
        self._writer = None
        self._count  = 0

    def start(self, folder: Path, stem: str) -> Path:
        import tifffile
        folder.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{stem}_pupil_{ts}.tif"
        self._writer = tifffile.TiffWriter(str(path), bigtiff=True)
        self._count  = 0
        return path

    def write(self, frame: np.ndarray) -> None:
        if self._writer:
            self._writer.write(frame)
            self._count += 1

    def stop(self) -> int:
        if self._writer:
            self._writer.close()
            self._writer = None
        return self._count

    @property
    def is_recording(self) -> bool:
        return self._writer is not None


class TrackingLog:
    """Writes PupilResult rows to CSV keyed by frame index."""

    def __init__(self) -> None:
        self._fh = self._writer = None

    def start(self, folder: Path, stem: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{stem}_pupil_tracking_{ts}.csv"
        self._fh     = open(path, "w", newline="")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["frame", "cx_px", "cy_px", "r_px", "confidence"])
        return path

    def write(self, frame_idx: int, result) -> None:
        if self._writer:
            self._writer.writerow([
                frame_idx,
                f"{result.center_x:.2f}" if result.center_x is not None else "",
                f"{result.center_y:.2f}" if result.center_y is not None else "",
                f"{result.radius:.2f}"   if result.radius   is not None else "",
                f"{result.confidence:.3f}",
            ])

    def stop(self) -> None:
        if self._fh:
            self._fh.flush(); self._fh.close()
            self._fh = self._writer = None
