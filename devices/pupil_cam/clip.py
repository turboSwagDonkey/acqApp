"""Pupil footage from a file, frame by frame: what Pupil review reads. No Qt.

    .avi          uncompressed AVI (avi.AviReader); a recording rolled into
                  `<stem>_002.avi`, `_003`... reads as one clip
    .tif / .tiff  a 2-D image stack (older sessions saved the pupil so)

All give `len()`, `luma(i)` (H, W) uint8, `width`, `height`, `hz`, `path`
and `describe()`. `recorded_clip(folder)` finds a session's pupil footage.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from acqApp.devices.pupil_cam.avi import AviReader

STREAM = "pupil_cam"
FILE_FILTER = ("Pupil footage (*.avi *.tif *.tiff);;Uncompressed AVI (*.avi);;"
               "TIFF stack (*.tif *.tiff);;All files (*)")


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


class SegmentedAvi:
    """`<stem>.avi` plus its `<stem>_002.avi`, `_003`... as one clip."""

    def __init__(self, parts: list[Path]) -> None:
        self._parts = [AviReader(p) for p in parts]
        first = self._parts[0]
        self.path = first.path
        self.width, self.height, self.hz = first.width, first.height, first.hz
        self._starts = np.cumsum([0] + [len(p) for p in self._parts])

    def __len__(self) -> int:
        return int(self._starts[-1])

    def luma(self, i: int) -> np.ndarray:
        if not 0 <= i < len(self):
            raise IndexError(i)
        k = int(np.searchsorted(self._starts, i, side="right")) - 1
        return self._parts[k].luma(i - int(self._starts[k]))

    def describe(self) -> str:
        return (f"{self.path.name} (+{len(self._parts) - 1} parts): "
                f"{self.width}x{self.height}, {len(self)} frames @ {self.hz:.2f} Hz")


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


def _parts(path: Path) -> list[Path]:
    out, k = [path], 2
    while (nxt := path.with_name(f"{path.stem}_{k:03d}{path.suffix}")).is_file():
        out.append(nxt)
        k += 1
    return out


def open_clip(path: str | Path):
    """The right reader for `path`, by extension."""
    path = Path(path)
    if path.suffix.lower() in (".tif", ".tiff"):
        return TiffReader(path)
    parts = _parts(path)
    return SegmentedAvi(parts) if len(parts) > 1 else AviReader(path)


def recorded_clip(session: str | Path | None) -> Path | None:
    """A session folder's pupil footage, `<name>_pupil_cam.avi` (or .tiff
    from before it was AVI). None if there is none."""
    if session is None:
        return None
    p = Path(session)
    if not p.is_dir():
        return p if p.is_file() else None
    for ext in (".avi", ".tiff"):
        f = p / f"{p.name}_{STREAM}{ext}"
        if f.is_file():
            return f
    return None
