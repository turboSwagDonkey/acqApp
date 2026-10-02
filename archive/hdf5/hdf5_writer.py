"""HDF5Writer — retired 2026-10-01 (see README.md here). Not imported."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from acqApp.acq.writer import Writer


def attr_value(v: Any) -> Any:
    """Metadata in its own HDF5 type (str() once filed `False` as truthy
    "False"). None -> "": HDF5 has no null, and 0.0 would read as measured."""
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str, np.generic, np.ndarray)):
        return v
    return str(v)


def _attrs(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: attr_value(v) for k, v in metadata.items()}


class HDF5Writer(Writer):
    """Any number of image/scalar streams in one HDF5 file.

    Images are uncompressed: gzip can't keep up with noisy 16-bit data, and
    compression also disables the direct-chunk path. Full frame on the NVMe
    (2026-08-25; the disk itself writes 2700 MB/s):

        `dset[i] = frame`              1304 MB/s
        direct chunk write             2696 MB/s
        + Recorder/ring, 106 Hz        2225 MB/s   100 % kept (59 via dset[i])
        + Recorder/ring, saturated     2464 MB/s

    Cache size, growth block, preallocation, alignment, VFD and multi-frame
    chunks each moved it under 3 %.
    """

    _CHUNK_SCALAR = 1024        # scalar samples per chunk / growth block
    _IMG_CHUNK_BYTES = 8 << 20
    _MIN_GROW_BYTES = 64 << 20
    _MIN_GROW_FRAMES = 16       # a ~20 MB full frame would resize every 3 frames

    def __init__(self, compression: str | None = None,
                 compression_opts: Any = None, overwrite: bool = False) -> None:
        self._file = None
        self._streams: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._compression = compression
        self._compression_opts = compression_opts
        self._overwrite = overwrite

    def open(self, path: Path, metadata: dict[str, Any]) -> None:
        """Mode "x": raises FileExistsError rather than clobber a session."""
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
            if i >= st["cap"]:
                cap = st["cap"] + st["block"]
                st["ts"].resize((cap,))
                st["data"].resize((cap,) + st["shape"])
                st["cap"] = cap

            st["ts"][i] = timestamp
            if not st["image"]:
                st["data"][i] = float(data)
            elif st["direct"] and self._writable_chunk(st, data):
                # One frame is one chunk: hand HDF5 the frame's own buffer.
                st["data"].id.write_direct_chunk(
                    (i,) + st["zero_offset"], memoryview(data).cast("B"))
            else:
                st["data"][i] = data
            st["idx"] = i + 1

    @staticmethod
    def _writable_chunk(st: dict[str, Any], data: Any) -> bool:
        """A direct write converts nothing and accepts an undersized buffer
        silently (the file then crashes its reader), so match exactly."""
        return (data.shape == st["shape"] and data.dtype == st["dtype"]
                and data.flags.c_contiguous)

    def _create_stream(self, stream: str, data: Any, is_image: bool) -> dict[str, Any]:
        g = self._file.require_group(stream)
        ts = g.create_dataset(
            "timestamps", shape=(0,), maxshape=(None,),
            dtype="float64", chunks=(self._CHUNK_SCALAR,), fillvalue=np.nan)
        if is_image:
            shape = tuple(data.shape)
            frame_bytes = max(data.nbytes, 1)
            chunk_frames = max(1, min(16, self._IMG_CHUNK_BYTES // frame_bytes))
            chunk_bytes = chunk_frames * frame_bytes
            # A multi-frame chunk is touched once per frame; cache a few.
            dset = g.create_dataset(
                "frames", shape=(0,) + shape, maxshape=(None,) + shape,
                dtype=data.dtype, chunks=(chunk_frames,) + shape,
                compression=self._compression,
                compression_opts=self._compression_opts,
                rdcc_nbytes=max(4 * chunk_bytes, 8 << 20),
                rdcc_nslots=4093)
            # Grow in large steps: a resize is dataset-wide metadata.
            grow = max(chunk_frames, self._MIN_GROW_FRAMES,
                       self._MIN_GROW_BYTES // frame_bytes)
            grow = (grow // chunk_frames) * chunk_frames
            st = {"image": True, "shape": shape, "dtype": data.dtype,
                  "ts": ts, "data": dset, "idx": 0, "cap": 0, "block": grow,
                  "direct": chunk_frames == 1 and self._compression is None,
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
                for st in self._streams.values():     # trim the preallocated tail
                    n = st["idx"]
                    st["ts"].resize((n,))
                    st["data"].resize((n,) + st["shape"])
                self._file.flush()
                self._file.close()
                self._file = None
