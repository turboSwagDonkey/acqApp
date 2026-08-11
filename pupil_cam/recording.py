"""
Standalone recording backends for the pupil-cam bring-up harness
(pupil_cam/_toy.py).

NOT used by the main app — that records every stream through acq/ (Recorder +
HDF5Writer) on the shared clock. FrameWriter streams frames to a growable HDF5
stack; TrackingLog appends per-frame pupil detections to a CSV.
"""
from __future__ import annotations
import csv
from pathlib import Path

import numpy as np


class FrameWriter:
    def __init__(self) -> None:
        self._file = None
        self._dset = None
        self._n = 0

    @property
    def is_recording(self) -> bool:
        return self._file is not None

    def start(self, folder: Path, name: str) -> Path:
        import h5py
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}_frames.h5"
        self._file = h5py.File(path, "w")
        self._dset = None
        self._n = 0
        return path

    def write(self, frame: np.ndarray) -> None:
        if self._file is None:
            return
        if self._dset is None:
            self._dset = self._file.create_dataset(
                "frames", shape=(0,) + frame.shape, maxshape=(None,) + frame.shape,
                dtype=frame.dtype, chunks=(1,) + frame.shape,
                compression="gzip", compression_opts=1)
        self._dset.resize((self._n + 1,) + frame.shape)
        self._dset[self._n] = frame
        self._n += 1

    def stop(self) -> int:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._dset = None
        return self._n


class TrackingLog:
    def __init__(self) -> None:
        self._fh = None
        self._writer = None
        self._n = 0

    @property
    def is_recording(self) -> bool:
        return self._fh is not None

    def start(self, folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}_tracking.csv"
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(
            ["frame", "center_x", "center_y", "radius", "confidence"])
        self._n = 0
        return path

    def write(self, frame_index: int, result) -> None:
        if self._writer is not None:
            self._writer.writerow([frame_index, result.center_x, result.center_y,
                                   result.radius, result.confidence])
            self._n += 1

    def stop(self) -> int:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._writer = None
        return self._n
