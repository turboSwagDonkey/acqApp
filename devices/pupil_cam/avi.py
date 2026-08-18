"""Uncompressed-AVI reader — enough of RIFF to replay recorded pupil footage.

Not a video library. It handles the formats the rig's own capture software
writes, all of which store raw pixels per frame, so the "decode" is a reshape:

    IYUV / I420 / YV12   planar YUV 4:2:0 — the Y plane IS the grayscale frame
    Y800 / GREY / Y8     8-bit luma, already what the tracker wants
    BI_RGB 8/24/32       uncompressed DIB, bottom-up, BGR → luma

Anything genuinely compressed (MJPG, H.264, …) raises with the FourCC named:
this venv is Python 3.14, where cv2 has no wheels and imageio/av are absent, so
there is nothing to hand it to. See PLAN.md §6.

numpy memmap, so a 500 MB clip costs nothing to open and frames are views.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

# Y-plane-first planar YUV: 4:2:0 chroma follows and is ignored.
_PLANAR_Y = {b"IYUV", b"I420", b"YV12"}
# 8-bit luma FourCCs — stored top-down, unlike BI_RGB below.
_GRAY = {b"Y800", b"GREY", b"Y8  "}
# BI_RGB: compression 0, and the only layout here that is bottom-up.
_BI_RGB = b"\x00\x00\x00\x00"


class AviReader:
    """Frame-indexed reader over an uncompressed AVI. `luma(i)` is (H, W) uint8."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._buf = np.memmap(self.path, dtype=np.uint8, mode="r")
        if self._tag(0) != b"RIFF" or self._tag(8) != b"AVI ":
            raise ValueError(f"{self.path.name}: not a RIFF AVI")

        head = bytes(self._buf[:16384])
        o = head.find(b"strf")
        if o < 0:
            raise ValueError(f"{self.path.name}: no stream format chunk")
        _sz, w, h, _planes, bits = struct.unpack_from("<IiiHH", head, o + 8)
        self.fourcc = struct.unpack_from("<4s", head, o + 8 + 16)[0]
        self.width, self.bits = int(w), int(bits)
        # biHeight < 0 means top-down; BI_RGB is otherwise stored bottom-up.
        self.height = int(abs(h))
        self._flip = self.fourcc == _BI_RGB and h > 0

        o = head.find(b"avih")
        us = struct.unpack_from("<I", head, o + 8)[0] if o >= 0 else 0
        self.fps = 1e6 / us if us else 0.0

        self._ybytes = self.width * self.height
        if self.fourcc in _PLANAR_Y:
            self._kind = "planar"
        elif self.fourcc in _GRAY or self.fourcc == _BI_RGB:
            if bits not in (8, 24, 32):
                raise ValueError(f"{self.path.name}: uncompressed {bits}-bit "
                                 f"is not supported")
            self._kind = "dib"
            self._px = bits // 8
        else:
            raise ValueError(
                f"{self.path.name}: compressed video ({self.fourcc.decode(errors='replace')})"
                f" — this venv has no decoder (no cv2/imageio/av, no ffmpeg). "
                f"Re-export as uncompressed/IYUV, or install imageio-ffmpeg.")

        self._offsets = self._index()
        if not self._offsets:
            raise ValueError(f"{self.path.name}: no frames found in movi")

    def _tag(self, off: int) -> bytes:
        return bytes(self._buf[off:off + 4])

    def _u32(self, off: int) -> int:
        return int(struct.unpack("<I", bytes(self._buf[off:off + 4]))[0])

    def _index(self) -> list[int]:
        """Pixel-data offset per frame, by walking the `movi` list."""
        end = len(self._buf)
        pos, movi = 12, None
        while pos + 8 <= end:
            sz = self._u32(pos + 4)
            if self._tag(pos) == b"LIST" and self._tag(pos + 8) == b"movi":
                movi = (pos + 12, min(end, pos + 8 + sz))
                break
            pos += 8 + sz + (sz & 1)
        if movi is None:
            raise ValueError(f"{self.path.name}: no movi list")

        need = self._ybytes if self._kind == "planar" else self._ybytes * self._px
        offs: list[int] = []
        pos, stop = movi
        while pos + 8 <= stop:
            sz = self._u32(pos + 4)
            # `##db`/`##dc` = a stream's data chunk; skip the index and any junk.
            if self._tag(pos)[2:] in (b"db", b"dc") and sz >= need:
                offs.append(pos + 8)
            pos += 8 + sz + (sz & 1)
        return offs

    def __len__(self) -> int:
        return len(self._offsets)

    def luma(self, i: int) -> np.ndarray:
        """Frame `i` as (H, W) uint8 grayscale."""
        o = self._offsets[i]
        if self._kind == "planar":
            return self._buf[o:o + self._ybytes].reshape(self.height, self.width)

        raw = self._buf[o:o + self._ybytes * self._px]
        if self._px == 1:
            f = raw.reshape(self.height, self.width)
        else:
            bgr = raw.reshape(self.height, self.width, self._px)[:, :, :3]
            # Rec.601 luma in uint8, integer weights to stay off floats.
            f = ((bgr[:, :, 0].astype(np.uint16) * 29
                  + bgr[:, :, 1].astype(np.uint16) * 150
                  + bgr[:, :, 2].astype(np.uint16) * 77) >> 8).astype(np.uint8)
        return f[::-1] if self._flip else f

    def __iter__(self):
        for i in range(len(self)):
            yield self.luma(i)

    def describe(self) -> str:
        return (f"{self.path.name}: {self.width}x{self.height} "
                f"{self.fourcc.decode(errors='replace').strip()} "
                f"{len(self)} frames @ {self.fps:.2f} fps")
