"""Pupil footage from a file, frame by frame: what Pupil review reads. No Qt.

    .avi          uncompressed AVI (avi.AviReader)
    .h5           an acqApp session: the "pupil_cam" stream
    .tif / .tiff  an acqApp split session's pupil stack (or any 2-D stack)

All give `len()`, `luma(i)` (H, W) uint8, `width`, `height`, `hz`, `path`
and `describe()`. `recorded_clip(path)` finds the pupil footage of a session
acqApp just wrote, whichever writer it used.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from acqApp.devices.pupil_cam.avi import AviReader

STREAM = "pupil_cam"
FILE_FILTER = ("Pupil footage (*.avi *.h5 *.tif *.tiff);;Uncompressed AVI (*.avi);;"
               "acqApp session (*.h5);;TIFF stack (*.tif *.tiff);;All files (*)")


def _hz(ts: np.ndarray) -> float:
    d = np.diff(ts[np.isfinite(ts)])
    d = d[d > 0]
    return float(1.0 / np.median(d)) if d.size else 0.0


class _To8:
    """Deeper-than-8-bit frames scaled to uint8 by one factor for the whole
    clip (from its first frames), so brightness doesn't jump frame to frame."""

    def __init__(self, sample: np.ndarray) -> None:
        top = float(np.percentile(sample, 99.9)) if sample.size else 255.0
        self._k = 255.0 / max(top, 1.0)

    def __call__(self, f: np.ndarray) -> np.ndarray:
        if f.dtype == np.uint8:
            return f
        return np.clip(f.astype(np.float32) * self._k, 0, 255).astype(np.uint8)


class H5Reader:
    """The pupil stream of an acqApp .h5 session (read-only, kept open)."""

    def __init__(self, path: str | Path, stream: str = STREAM) -> None:
        import h5py
        self.path = Path(path)
        self._f = h5py.File(self.path, "r")
        if stream not in self._f or "frames" not in self._f[stream]:
            self._f.close()
            raise ValueError(f"{self.path.name}: no '{stream}' frames in it")
        g = self._f[stream]
        self._frames = g["frames"]
        ts = np.asarray(g["timestamps"][:], dtype=float) if "timestamps" in g else np.array([])
        # Rows past the last written one are NaN-stamped padding.
        n = int(np.isfinite(ts).sum()) if ts.size else self._frames.shape[0]
        self._n = min(n, self._frames.shape[0])
        self.height, self.width = (int(v) for v in self._frames.shape[1:3])
        self.hz = _hz(ts[:self._n]) if ts.size else 0.0
        self._to8 = _To8(np.asarray(self._frames[0]) if self._n else np.zeros(1))

    def __len__(self) -> int:
        return self._n

    def luma(self, i: int) -> np.ndarray:
        f = np.asarray(self._frames[i])
        return self._to8(f if f.ndim == 2 else f[..., 0])

    def describe(self) -> str:
        return (f"{self.path.name}: {self.width}x{self.height} session stream, "
                f"{len(self)} frames @ {self.hz:.2f} Hz")


class TiffReader:
    """A 2-D image stack, one page per frame."""

    def __init__(self, path: str | Path) -> None:
        import tifffile
        self.path = Path(path)
        self._tif = tifffile.TiffFile(self.path)
        pages = self._tif.pages
        if not len(pages):
            raise ValueError(f"{self.path.name}: no images in it")
        first = pages[0].asarray()
        self.height, self.width = (int(v) for v in first.shape[:2])
        self.hz = 0.0
        self._n = len(pages)
        self._to8 = _To8(first)

    def __len__(self) -> int:
        return self._n

    def luma(self, i: int) -> np.ndarray:
        f = self._tif.pages[i].asarray()
        return self._to8(f if f.ndim == 2 else f[..., 0])

    def describe(self) -> str:
        return (f"{self.path.name}: {self.width}x{self.height} TIFF stack, "
                f"{len(self)} frames")


def open_clip(path: str | Path):
    """The right reader for `path`, by extension."""
    suffix = Path(path).suffix.lower()
    if suffix == ".h5":
        return H5Reader(path)
    if suffix in (".tif", ".tiff"):
        return TiffReader(path)
    return AviReader(path)


def recorded_clip(session: str | Path | None) -> Path | None:
    """The pupil footage of a session acqApp wrote: the .h5 itself, or the
    split folder's `<name>_pupil_cam.tiff`. None if there is none."""
    if session is None:
        return None
    p = Path(session)
    if p.is_dir():
        tif = p / f"{p.name}_{STREAM}.tiff"
        return tif if tif.is_file() else None
    return p if p.is_file() else None
