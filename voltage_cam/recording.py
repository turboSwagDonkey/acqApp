"""
Voltage-imaging camera — TIFF recording manager.

Pure data class — no Qt dependency.  The caller decides when to call
start() / write() / stop() based on UI events or sync triggers.
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path

import numpy as np


class RecordingManager:
    """Incremental BigTIFF writer with frame-count target."""

    def __init__(self) -> None:
        self._writer  = None
        self._path:   Path | None = None
        self._count   = 0
        self._target  = 0

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self, folder: Path, stem: str, n_frames: int) -> Path:
        """
        Open a new BigTIFF at `folder/stem_<timestamp>.tif`.
        Returns the file path so the caller can display it.
        Raises if a recording is already in progress.
        """
        if self._writer is not None:
            raise RuntimeError("Already recording — call stop() first")

        import tifffile
        folder.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{stem}_{ts}.tif"
        self._writer = tifffile.TiffWriter(str(path), bigtiff=True)
        self._path   = path
        self._count  = 0
        self._target = n_frames
        return path

    def write(self, frame: np.ndarray) -> bool:
        """
        Write one frame. Returns True when the target frame count is reached
        so the caller knows to call stop().
        """
        if self._writer is None:
            return False
        self._writer.write(frame)
        self._count += 1
        return self._count >= self._target > 0

    def stop(self) -> int:
        """Flush and close; returns number of frames written."""
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        n = self._count
        self._count = self._target = 0
        return n

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._writer is not None

    @property
    def frame_count(self) -> int:
        return self._count

    @property
    def target(self) -> int:
        return self._target

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def progress(self) -> float:
        """0.0–1.0; 0 if target is unlimited (0)."""
        return self._count / self._target if self._target > 0 else 0.0

    # ── Folder helper (static) ─────────────────────────────────────────────────

    @staticmethod
    def build_path(drive: str, probe: str, date: str, fov: int, trial: int) -> Path:
        """
        Reconstruct the LabVIEW-style folder structure:
            <drive>/<probe>/<date>/FOV<fov>_T<trial>/FOV<fov>_T<trial>
        """
        stem = f"FOV{fov}_T{trial}"
        return Path(f"{drive}\\{probe}\\{date}\\{stem}\\{stem}")
