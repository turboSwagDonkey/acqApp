"""Writer ABC + HDF5Writer.

A Writer persists (stream, timestamp, data) tuples, swappable without touching
acquisition code. Timestamps all come from the SessionClock, so every stream
shares one origin.

Metadata lands in the root attributes in its OWN type (`attr_value`), so
analysis reads `f.attrs["wheel_volts_per_rev"] * x` instead of parsing strings.

Layout, one group per stream, created lazily on first write:
  /<stream>/timestamps   float64 (N,)         seconds since session start
  /<stream>/frames       <dtype> (N, H, W)    image streams (camera, pupil)
  /<stream>/values       float64 (N,)         scalar streams (encoder, puffer)

Datasets grow a block at a time to amortise the resize, but each sample is
written immediately. `timestamps` has a NaN fill, so a killed process leaves
identifiable tail rows and every written row is recoverable; a clean close
trims to exact length, so a normal file has no NaNs.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np


def attr_value(v: Any) -> Any:
    """Coerce one metadata value into something HDF5 stores in its own type.

    Everything used to go through `str()`, so `emulated` landed as `"False"` —
    truthy. `None` becomes `""`: HDF5 has no null, and 0.0 for an unset
    volts-per-rev is indistinguishable from a measured zero.
    """
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str, np.generic, np.ndarray)):
        return v            # bool before int: bool IS an int, and h5py keeps it
    return str(v)           # Path, enum, dataclass, anything else


def _attrs(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: attr_value(v) for k, v in metadata.items()}


def _json_value(v: Any) -> Any:
    """Coerce one metadata value into something `json.dump` serializes
    natively. Unlike `attr_value` (HDF5's flat-attribute model, where a
    dict has nowhere to go but `str()`), this preserves dict/list
    structure — the routine protocol (`Routine.to_dict()`) is a nested
    dict and should read back as one in the settings JSON, not a
    stringified blob nobody can `json.load()` back into anything useful.
    """
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, dict):
        return {k: _json_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_value(x) for x in v]
    return str(v)           # Path, enum, dataclass, anything else


class Writer(ABC):
    @abstractmethod
    def open(self, path: Path, metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    def write(self, stream: str, timestamp: float, data: Any) -> None:
        """Persist one sample. `data` is an ndarray (image) or a scalar."""
        ...

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        """Add or overwrite file metadata after open(). Optional.

        Some facts are known only once the recording is over — the camera's
        timebase, how much was shed — and belong in the file rather than in a
        status message that scrolls away.
        """

    @abstractmethod
    def close(self) -> None: ...


class HDF5Writer(Writer):
    """Streams any number of named image/scalar channels into one HDF5 file.

    Images are UNCOMPRESSED: gzip manages a fraction of the rate on noisy
    16-bit data, stalling the writer and backing up the ring. `compression=`
    also gives up the direct-chunk path below, which is the fast one.

    Full frame on D: (KC3000 NVMe), 2026-08-25; the rig kept 53 % of a bin-1
    stream before it (2026-08-17). A plain file writes 2700 MB/s, so the disk
    was never the wall — the slice assignment was:

        `dset[i] = frame`              1304 MB/s
        direct chunk write             2696 MB/s
        + Recorder/ring, 106 Hz        2225 MB/s   100 % kept (was 59)
        + Recorder/ring, saturated     2464 MB/s

    Chunk cache size, growth block, preallocation, 1/4 MB alignment, the Windows
    VFD and multi-frame chunks were each measured: none moves it over 3 %.
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

        Mode "x", not "w": an existing session file is hours of animal time with
        no undo. `overwrite=True` for the rare deliberate clobber.
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
            st = self._streams.get(stream)
            if st is None:
                is_image = isinstance(data, np.ndarray) and data.ndim >= 2
                st = self._create_stream(stream, data, is_image)

            i = st["idx"]
            if i >= st["cap"]:                       # grow capacity by one block
                cap = st["cap"] + st["block"]
                st["ts"].resize((cap,))
                st["data"].resize((cap,) + st["shape"])
                st["cap"] = cap

            st["ts"][i] = timestamp
            if not st["image"]:
                st["data"][i] = float(data)
            elif st["direct"] and self._writable_chunk(st, data):
                # One frame IS one chunk, so hand HDF5 the frame's own
                # buffer: no cache copy, no conversion. The slice assignment
                # below is why bin 1 dropped half its frames.
                st["data"].id.write_direct_chunk(
                    (i,) + st["zero_offset"], memoryview(data).cast("B"))
            else:
                st["data"][i] = data
            st["idx"] = i + 1

    @staticmethod
    def _writable_chunk(st: dict[str, Any], data: Any) -> bool:
        """Is `data` byte-for-byte what this dataset's chunk holds?

        A direct write converts nothing, and an undersized one is accepted
        silently — the file then kills whatever reads it (access violation,
        `test_writer_chunks`). Anything else falls to the slice assignment.
        """
        return (data.shape == st["shape"] and data.dtype == st["dtype"]
                and data.flags.c_contiguous)

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
            # A multi-frame chunk is touched once per frame before it's
            # complete, so hold a few or every write evicts and re-reads it.
            # Unused on the direct path, which never enters the cache.
            dset = g.create_dataset(
                "frames", shape=(0,) + shape, maxshape=(None,) + shape,
                dtype=data.dtype, chunks=(chunk_frames,) + shape,
                compression=self._compression,
                compression_opts=self._compression_opts,
                rdcc_nbytes=max(4 * chunk_bytes, 8 << 20),
                rdcc_nslots=4093)
            # Independent of chunk size: a resize is dataset-wide metadata,
            # so every 16 frames would cost hundreds of resizes/s.
            grow = max(chunk_frames,
                       (self._MIN_GROW_BYTES // max(frame_bytes, 1)) or 1)
            grow = (grow // chunk_frames) * chunk_frames or chunk_frames
            st = {"image": True, "shape": shape, "dtype": data.dtype,
                  "ts": ts, "data": dset, "idx": 0, "cap": 0, "block": grow,
                  # write_direct_chunk writes one chunk of raw bytes: only
                  # valid where a frame is exactly a chunk and no filter is
                  # meant to run on it.
                  "direct": chunk_frames == 1 and self._compression is None,
                  # The chunk-index offset's trailing zeros, precomputed once
                  # rather than rebuilt into a fresh tuple on every write() —
                  # the direct path exists specifically to avoid per-frame
                  # overhead.
                  "zero_offset": (0,) * len(shape)}
        else:
            dset = g.create_dataset(
                "values", shape=(0,), maxshape=(None,),
                dtype="float64", chunks=(self._CHUNK_SCALAR,))
            st = {"image": False, "shape": (), "dtype": np.dtype("float64"),
                  "ts": ts, "data": dset, "idx": 0, "cap": 0,
                  "block": self._CHUNK_SCALAR, "direct": False}
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


class TiffFileWriter:
    """One image stream's frames as a multi-page TIFF, kept open for the
    whole session. Not a `Writer` itself — `SplitWriter` owns one of these
    per image stream, the way `HDF5Writer` owns one group per stream.

    TIFF has no per-frame timestamp slot worth trusting across readers, so
    timestamps go in a small sidecar CSV (frame index -> timestamp)
    instead of a custom TIFF tag scheme nothing else would understand.
    """

    def __init__(self, path: Path) -> None:
        import tifffile
        self._tif = tifffile.TiffWriter(path, bigtiff=True)
        ts_path = path.with_name(path.stem + "_timestamps.csv")
        self._ts_file = open(ts_path, "w", newline="", encoding="utf-8")
        self._ts_file.write("frame,timestamp\n")
        self._n = 0

    def write(self, timestamp: float, data: np.ndarray) -> None:
        self._tif.write(data, contiguous=True)
        self._ts_file.write(f"{self._n},{timestamp!r}\n")
        self._n += 1

    def close(self) -> None:
        self._tif.close()
        self._ts_file.close()


class LongCsvWriter:
    """One CSV for every scalar/event stream in a session:
    `timestamp,stream,value,routine_step`. Long format — one row per
    sample, not one column per stream — so streams sampled at very
    different rates (wheel ~120 Hz, puffer sparse events, pupil fit
    ~7-20 Hz) never need resampling or alignment to share a file.

    `routine_step` is carried on every row, not just the `routine` stream's
    own: `adapters/routines.py`'s `_put()` already encodes step boundaries
    as +/-(index+1) on that one stream (positive = entering, negative =
    leaving) — this decodes that and stamps whichever step is currently
    open onto every other row too, so filtering the CSV by routine_step
    needs no join against a separate boundaries table.
    """

    _HEADER = "timestamp,stream,value,routine_step\n"

    def __init__(self, path: Path) -> None:
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._file.write(self._HEADER)
        self._step = ""

    def write(self, stream: str, timestamp: float, data: Any) -> None:
        if stream == "routine":
            n = int(data)
            idx = str(abs(n) - 1)
            row_step = idx                     # this row names step idx either way
            self._step = idx if n > 0 else ""   # what applies to rows AFTER this one
        else:
            row_step = self._step
        self._file.write(f"{timestamp!r},{stream},{float(data)!r},{row_step}\n")

    def close(self) -> None:
        self._file.close()


class SplitWriter(Writer):
    """Each stream in its own file instead of one composite .h5: a TIFF
    stack per image stream, one shared `LongCsvWriter` for every scalar
    stream, one JSON for settings (including the full routine protocol,
    not just its boundary markers — see `RoutinesModule.metadata()`).

    `path` passed to `open()` is a DIRECTORY (the session folder), not a
    file — `SaveConfig.resolve_dir()` is what `main.py` resolves it from.
    """

    def __init__(self) -> None:
        self._dir: Path | None = None
        self._stem = ""
        self._tiffs: dict[str, TiffFileWriter] = {}
        self._csv: LongCsvWriter | None = None
        self._metadata: dict[str, Any] = {}
        self._lock = threading.Lock()

    def open(self, path: Path, metadata: dict[str, Any]) -> None:
        # Mirrors HDF5Writer's mode "x": an existing session folder is
        # hours of animal time with no undo.
        path.mkdir(parents=True, exist_ok=False)
        self._dir = path
        self._stem = path.name
        self._metadata = dict(metadata)
        self._write_json()

    def _write_json(self) -> None:
        import json
        p = self._dir / f"{self._stem}_settings.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump({k: _json_value(v) for k, v in self._metadata.items()},
                      f, indent=2, sort_keys=True)

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            if self._dir is None:
                return
            self._metadata.update(metadata)
            self._write_json()

    def write(self, stream: str, timestamp: float, data: Any) -> None:
        with self._lock:
            if self._dir is None:
                return
            if isinstance(data, np.ndarray) and data.ndim >= 2:
                w = self._tiffs.get(stream)
                if w is None:
                    w = TiffFileWriter(self._dir / f"{self._stem}_{stream}.tiff")
                    self._tiffs[stream] = w
                w.write(timestamp, data)
            else:
                if self._csv is None:
                    self._csv = LongCsvWriter(self._dir / f"{self._stem}_data.csv")
                self._csv.write(stream, timestamp, data)

    def close(self) -> None:
        with self._lock:
            for w in self._tiffs.values():
                w.close()
            self._tiffs = {}
            if self._csv is not None:
                self._csv.close()
                self._csv = None
            self._dir = None
