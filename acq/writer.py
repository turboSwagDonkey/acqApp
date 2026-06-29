"""
Writer ABC + HDF5Writer.

A Writer receives (timestamp, data) tuples and persists them. Swap
implementations without touching acquisition code.

HDF5 layout:
  /camera/timestamps   float64 1-D, seconds since session start
  /camera/frames       uint16  (N, H, W)
  /encoder/timestamps  float64 1-D
  /encoder/voltage     float64 1-D

Datasets are created with chunking + gzip so partial writes are valid on crash.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np


class Writer(ABC):
    @abstractmethod
    def open(self, path: Path, metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    def write_frame(self, timestamp: float, frame: np.ndarray) -> None: ...

    @abstractmethod
    def write_encoder(self, timestamp: float, voltage: float) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class HDF5Writer(Writer):
    """Streams camera frames and encoder samples into a single HDF5 file."""

    _CHUNK_FRAMES = 16          # frames per HDF5 chunk
    _CHUNK_ENCODER = 1024       # encoder samples per HDF5 chunk

    def __init__(self) -> None:
        self._file = None
        self._cam_ts = None
        self._cam_frames = None
        self._enc_ts = None
        self._enc_volt = None
        self._frame_idx = 0
        self._enc_idx = 0
        self._lock = threading.Lock()

    def open(self, path: Path, metadata: dict[str, Any]) -> None:
        import h5py
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = h5py.File(path, "w")
        self._file.attrs.update({k: str(v) for k, v in metadata.items()})
        self._frame_idx = 0
        self._enc_idx = 0
        # Datasets created lazily on first write so we know frame shape.

    def _init_camera_datasets(self, frame: np.ndarray) -> None:
        import h5py
        g = self._file.require_group("camera")
        H, W = frame.shape[-2], frame.shape[-1]
        self._cam_ts = g.create_dataset(
            "timestamps", shape=(0,), maxshape=(None,),
            dtype="float64", chunks=(self._CHUNK_FRAMES,))
        self._cam_frames = g.create_dataset(
            "frames", shape=(0, H, W), maxshape=(None, H, W),
            dtype=frame.dtype,
            chunks=(self._CHUNK_FRAMES, H, W),
            compression="gzip", compression_opts=1)

    def _init_encoder_datasets(self) -> None:
        g = self._file.require_group("encoder")
        self._enc_ts = g.create_dataset(
            "timestamps", shape=(0,), maxshape=(None,),
            dtype="float64", chunks=(self._CHUNK_ENCODER,))
        self._enc_volt = g.create_dataset(
            "voltage", shape=(0,), maxshape=(None,),
            dtype="float64", chunks=(self._CHUNK_ENCODER,))

    def write_frame(self, timestamp: float, frame: np.ndarray) -> None:
        with self._lock:
            if self._cam_frames is None:
                self._init_camera_datasets(frame)
            n = self._frame_idx + 1
            self._cam_ts.resize((n,))
            self._cam_frames.resize((n, frame.shape[-2], frame.shape[-1]))
            self._cam_ts[self._frame_idx] = timestamp
            self._cam_frames[self._frame_idx] = frame
            self._frame_idx = n

    def write_encoder(self, timestamp: float, voltage: float) -> None:
        with self._lock:
            if self._enc_volt is None:
                self._init_encoder_datasets()
            n = self._enc_idx + 1
            self._enc_ts.resize((n,))
            self._enc_volt.resize((n,))
            self._enc_ts[self._enc_idx] = timestamp
            self._enc_volt[self._enc_idx] = voltage
            self._enc_idx = n

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None
