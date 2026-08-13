"""
Writer ABC + HDF5Writer.

A Writer receives (stream, timestamp, data) tuples and persists them. Swap
implementations without touching acquisition code.

All timestamps come from the single session-wide SessionClock, so every
stream in the file shares one time origin (seconds since session start).

Session metadata lands in the root attributes in its OWN type (see
`attr_value`), so analysis reads `f.attrs["wheel_volts_per_rev"] * x` rather
than parsing strings.

HDF5 layout (one group per stream, created lazily on first write):
  /<stream>/timestamps   float64 (N,)         seconds since session start
  /<stream>/frames       <dtype> (N, H, W)    image streams (camera, pupil)
  /<stream>/values       float64 (N,)         scalar streams (encoder, puffer)

Throughput and crash-safety: datasets grow a *block* at a time to amortise the
resize cost at high rates, but each sample is written immediately. `timestamps`
is created with a NaN fill, so a process killed mid-block leaves identifiable
tail rows and every written row is recoverable. A clean close trims each dataset
to its exact length, so a normally-closed file has no NaNs.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np


def attr_value(v: Any) -> Any:
    """Coerce one metadata value into something HDF5 stores in its own type.

    Everything used to go through `str()`, so `wheel_volts_per_rev` landed as
    `"4.912"` and `emulated` as `"False"` — which is truthy. h5py stores ints,
    floats, bools and strings natively, so there was nothing to gain.

    `None` becomes `""`: HDF5 has no null, and an empty string reads as "not
    set" without pretending to be a number — 0.0 for an unset volts-per-rev
    would be indistinguishable from a measured zero.
    """
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str, np.generic, np.ndarray)):
        return v            # bool before int: bool IS an int, and h5py keeps it
    return str(v)           # Path, enum, dataclass, anything else


def _attrs(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: attr_value(v) for k, v in metadata.items()}


class Writer(ABC):
    @abstractmethod
    def open(self, path: Path, metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    def write(self, stream: str, timestamp: float, data: Any) -> None:
        """Persist one sample. `data` is an ndarray (image) or a scalar."""
        ...

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        """Add or overwrite file metadata after open().

        Some facts about a recording are only known once it is over — which
        timebase the camera actually gave us, how many samples were shed. They
        belong in the file rather than in a status-bar message that scrolls
        away. Optional: a Writer that cannot do this may ignore it.
        """

    @abstractmethod
    def close(self) -> None: ...


class HDF5Writer(Writer):
    """Streams any number of named image/scalar channels into one HDF5 file.

    Image streams are written UNCOMPRESSED by default. The voltage camera
    sustains ~330 MB/s at full frame (21 MB × 15.7 fps, which is USB3.1 Gen1
    saturation); single-threaded gzip manages roughly a third of that on noisy
    16-bit sensor data, so compressing here stalls the writer thread, backs the
    ring buffer up and drops frames. An NVMe SSD absorbs 330 MB/s comfortably.

    Pass `compression="gzip"` (or an `hdf5plugin` codec, once installed) only
    for slow streams or when disk space matters more than keeping up.
    """

    _CHUNK_SCALAR = 1024        # scalar samples per chunk / growth block
    _IMG_CHUNK_BYTES = 8 << 20  # aim for ~8 MB image chunks
    _MIN_GROW_BYTES = 64 << 20  # grow datasets in >=64 MB steps

    def __init__(self, compression: str | None = None,
                 compression_opts: Any = None, overwrite: bool = False) -> None:
        self._file = None
        self._streams: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._compression = compression
        self._compression_opts = compression_opts
        self._overwrite = overwrite

    def open(self, path: Path, metadata: dict[str, Any]) -> None:
        """Create the session file. Raises FileExistsError if `path` is taken.

        Mode "x", not "w": an existing session file is hours of animal time and
        there is no undo. Callers pick a free name (`SaveConfig.resolve(
        unique=True)`); this is the backstop for any that don't. Pass
        `overwrite=True` to the constructor for the rare deliberate clobber.
        """
        import h5py
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = h5py.File(path, "w" if self._overwrite else "x")
        self._file.attrs.update(_attrs(metadata))
        self._streams = {}

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            if self._file is not None:
                self._file.attrs.update(_attrs(metadata))

    def write(self, stream: str, timestamp: float, data: Any) -> None:
        with self._lock:
            if self._file is None:
                return
            is_image = isinstance(data, np.ndarray) and data.ndim >= 2
            st = self._streams.get(stream)
            if st is None:
                st = self._create_stream(stream, data, is_image)

            i = st["idx"]
            if i >= st["cap"]:                       # grow capacity by one block
                cap = st["cap"] + st["block"]
                st["ts"].resize((cap,))
                st["data"].resize((cap,) + st["shape"])
                st["cap"] = cap

            st["ts"][i] = timestamp
            if st["image"]:
                st["data"][i] = data
            else:
                st["data"][i] = float(data)
            st["idx"] = i + 1

    def _create_stream(self, stream: str, data: Any, is_image: bool) -> dict[str, Any]:
        g = self._file.require_group(stream)
        ts = g.create_dataset(
            "timestamps", shape=(0,), maxshape=(None,),
            dtype="float64", chunks=(self._CHUNK_SCALAR,), fillvalue=np.nan)
        if is_image:
            shape = tuple(data.shape)
            frame_bytes = data.dtype.itemsize * int(np.prod(shape))
            chunk_frames = max(1, min(16, self._IMG_CHUNK_BYTES // max(frame_bytes, 1)))
            chunk_bytes = chunk_frames * frame_bytes
            # Samples arrive one frame at a time, so a multi-frame chunk is
            # touched repeatedly before it is complete. Size the per-dataset
            # chunk cache to hold a few whole chunks, otherwise every write
            # evicts and re-reads (and, if compressed, re-deflates) the chunk.
            dset = g.create_dataset(
                "frames", shape=(0,) + shape, maxshape=(None,) + shape,
                dtype=data.dtype, chunks=(chunk_frames,) + shape,
                compression=self._compression,
                compression_opts=self._compression_opts,
                rdcc_nbytes=max(4 * chunk_bytes, 8 << 20),
                rdcc_nslots=4093)
            # Growth block is independent of the chunk size: resizing is a
            # metadata operation on the whole dataset, so doing it every 16
            # frames costs hundreds of resizes/s at the fast presets.
            grow = max(chunk_frames,
                       (self._MIN_GROW_BYTES // max(frame_bytes, 1)) or 1)
            grow = (grow // chunk_frames) * chunk_frames or chunk_frames
            st = {"image": True, "shape": shape, "ts": ts, "data": dset,
                  "idx": 0, "cap": 0, "block": grow}
        else:
            dset = g.create_dataset(
                "values", shape=(0,), maxshape=(None,),
                dtype="float64", chunks=(self._CHUNK_SCALAR,))
            st = {"image": False, "shape": (), "ts": ts, "data": dset,
                  "idx": 0, "cap": 0, "block": self._CHUNK_SCALAR}
        self._streams[stream] = st
        return st

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                # Trim each dataset to exactly what was written (drops the
                # pre-allocated tail so a cleanly closed file has no NaN rows).
                for st in self._streams.values():
                    n = st["idx"]
                    st["ts"].resize((n,))
                    st["data"].resize((n,) + st["shape"])
                self._file.flush()
                self._file.close()
                self._file = None
