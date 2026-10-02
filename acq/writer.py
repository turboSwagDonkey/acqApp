"""Writers: persist (stream, timestamp, data), all timestamps on the session
clock. One recording is one folder (`SessionWriter`):

  <name>_settings.json            every module's metadata, routine protocol
  <name>_data.csv                 every scalar stream, long format
  <name>_<stream>.tiff / .avi     an image stream (format per stream)
  <name>_<stream>_timestamps.csv  frame -> timestamp for that stream
  <name>_<stream>.dcimg           written by the ORCA itself, not here

The HDF5 writer is retired to archive/hdf5/.
"""
from __future__ import annotations

import json
import struct
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np


def _json_value(v: Any) -> Any:
    """Like attr_value, but keeps dict/list structure (the routine protocol)."""
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
    return str(v)


class Writer(ABC):
    @abstractmethod
    def open(self, path: Path, metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    def write(self, stream: str, timestamp: float, data: Any) -> None:
        """`data` is an ndarray (image) or a scalar."""

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        """Add or overwrite metadata after open(). Optional."""

    @abstractmethod
    def close(self) -> None: ...


class TiffFileWriter:
    """One image stream as a multi-page TIFF, timestamps in a sidecar CSV."""

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


class AviFileWriter:
    """One 8-bit image stream as uncompressed grayscale AVI (Y800), the
    format Pupil review reads; timestamps in a sidecar CSV.

    Plain AVI 1.0, so any player opens it. That caps a file near 4 GB, so it
    rolls to `<stem>_002.avi`, `_003`... before 2 GB (a full pupil frame at
    20 Hz fills one in ~40 s). The header's sizes are patched at close; a
    killed process leaves them as placeholders, which a reader that walks the
    chunks (devices/pupil_cam/avi.py) still reads up to the last whole frame.
    Deeper frames are shifted to 8 bits; colour keeps the first channel."""

    SEGMENT_BYTES = 1_900_000_000

    def __init__(self, path: Path) -> None:
        self._base = path
        ts_path = path.with_name(path.stem + "_timestamps.csv")
        self._ts_file = open(ts_path, "w", newline="", encoding="utf-8")
        self._ts_file.write("frame,timestamp\n")
        self._n = 0                 # frames over all segments
        self._seg = 0
        self._f = None
        self._ts: list[float] = []  # this segment's timestamps (for the rate)

    # ── one segment ──
    def _open_segment(self, h: int, w: int) -> None:
        self._seg += 1
        path = (self._base if self._seg == 1 else
                self._base.with_name(f"{self._base.stem}_{self._seg:03d}.avi"))
        self._f = open(path, "wb")
        self._h, self._w = h, w
        self._stride = (w + 3) & ~3             # DIB rows pad to 4 bytes
        self._frame_bytes = self._stride * h
        self._index: list[int] = []             # chunk offsets from 'movi'
        self._ts = []
        f = self._f
        f.write(b"RIFF\xff\xff\xff\xffAVI ")
        f.write(b"LIST" + struct.pack("<I", 4 + 64 + 12 + 64 + 48) + b"hdrl")
        self._avih = f.tell() + 8
        f.write(b"avih" + struct.pack("<I", 56) + struct.pack(
            "<14I", 0, 0, 0, 0x10, 0, 0, 1, self._frame_bytes, w, h, 0, 0, 0, 0))
        f.write(b"LIST" + struct.pack("<I", 4 + 64 + 48) + b"strl")
        self._strh = f.tell() + 8
        f.write(b"strh" + struct.pack("<I", 56) + struct.pack(
            "<4s4sIHHIIIIIIII4H", b"vids", b"Y800", 0, 0, 0, 0, 1, 1, 0, 0,
            self._frame_bytes, 0xFFFFFFFF, 0, 0, 0, w, h))
        f.write(b"strf" + struct.pack("<I", 40) + struct.pack(
            "<IiiHH4sIiiII", 40, w, h, 1, 8, b"Y800", self._frame_bytes,
            0, 0, 0, 0))
        f.write(b"LIST\xff\xff\xff\xffmovi")
        self._movi = f.tell() - 4               # offsets are from 'movi'

    def _close_segment(self) -> None:
        f = self._f
        if f is None:
            return
        movi_end = f.tell()
        f.write(b"idx1" + struct.pack("<I", 16 * len(self._index)))
        for off in self._index:
            f.write(b"00db" + struct.pack("<III", 0x10, off, self._frame_bytes))
        end = f.tell()
        n = len(self._index)
        d = np.diff(np.asarray(self._ts, float))
        d = d[np.isfinite(d) & (d > 0)]
        us = int(round(1e6 * float(np.median(d)))) if d.size else 0
        fps_milli = int(round(1e9 / us)) if us else 0
        f.seek(4)
        f.write(struct.pack("<I", end - 8))
        f.seek(self._avih)
        f.write(struct.pack("<I", us))
        f.seek(self._avih + 16)
        f.write(struct.pack("<I", n))
        f.seek(self._strh + 20)                 # dwScale, dwRate
        f.write(struct.pack("<II", 1000, fps_milli or 1))
        f.seek(self._strh + 32)                 # dwLength
        f.write(struct.pack("<I", n))
        f.seek(self._movi - 4)
        f.write(struct.pack("<I", movi_end - self._movi))
        f.close()
        self._f = None

    # ── the stream ──
    def write(self, timestamp: float, data: np.ndarray) -> None:
        if data.ndim == 3:
            data = data[..., 0]
        if data.dtype != np.uint8:
            bits = data.dtype.itemsize * 8
            data = (data >> (bits - 8)).astype(np.uint8)
        h, w = data.shape
        if (self._f is None or (h, w) != (self._h, self._w)
                or self._f.tell() + self._frame_bytes + 8
                + 16 * (len(self._index) + 1) > self.SEGMENT_BYTES):
            self._close_segment()
            self._open_segment(h, w)
        if self._stride != w:
            row = np.zeros((h, self._stride), np.uint8)
            row[:, :w] = data
            data = row
        f = self._f
        self._index.append(f.tell() - self._movi)
        f.write(b"00db" + struct.pack("<I", self._frame_bytes))
        f.write(np.ascontiguousarray(data).tobytes())
        self._ts.append(timestamp)
        self._ts_file.write(f"{self._n},{timestamp!r}\n")
        self._n += 1

    def close(self) -> None:
        self._close_segment()
        self._ts_file.close()


class LongCsvWriter:
    """Every scalar stream in one long-format CSV
    (`timestamp,stream,value,routine_step`), so different rates never need
    aligning. `routine_step` decodes the `routine` stream's +/-(n+1) edges
    and stamps the open Recording's index onto every row."""

    _HEADER = "timestamp,stream,value,routine_step\n"

    def __init__(self, path: Path) -> None:
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._file.write(self._HEADER)
        self._step = ""

    def write(self, stream: str, timestamp: float, data: Any) -> None:
        if stream == "routine":
            n = int(data)
            idx = str(abs(n) - 1)
            row_step = idx                      # this row names it either way
            self._step = idx if n > 0 else ""   # rows after it
        else:
            row_step = self._step
        self._file.write(f"{timestamp!r},{stream},{float(data)!r},{row_step}\n")

    def close(self) -> None:
        self._file.close()


class SessionWriter(Writer):
    """A session FOLDER: a TIFF or AVI per image stream, one long CSV for
    scalars, one settings JSON (with the full routine protocol).
    `image_formats` maps a stream to "avi" (default "tiff")."""

    def __init__(self, image_formats: dict[str, str] | None = None) -> None:
        self._dir: Path | None = None
        self._stem = ""
        self._formats = dict(image_formats or {})
        self._tiffs: dict[str, TiffFileWriter | AviFileWriter] = {}
        self._csv: LongCsvWriter | None = None
        self._metadata: dict[str, Any] = {}
        self._lock = threading.Lock()

    def open(self, path: Path, metadata: dict[str, Any]) -> None:
        path.mkdir(parents=True, exist_ok=False)   # never reuse a session folder
        self._dir = path
        self._stem = path.name
        self._metadata = dict(metadata)
        self._write_json()

    def _write_json(self) -> None:
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
                    base = self._dir / f"{self._stem}_{stream}"
                    w = (AviFileWriter(base.with_suffix(".avi"))
                         if self._formats.get(stream) == "avi"
                         else TiffFileWriter(base.with_suffix(".tiff")))
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


# The name it had while an .h5 was the default and a folder the option.
SplitWriter = SessionWriter
