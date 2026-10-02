"""Shared by the test_pupil*.py files: a real AVI writer, the synthetic
eyes the clips are made of, and a stand-in for EyeLoop."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def _chunk(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\0" * (len(payload) & 1))


def write_avi(path: Path, frames: list[bytes], w: int, h: int,
              fourcc: bytes, bits: int, us: int = 50000) -> Path:
    """A minimal but real RIFF AVI: hdrl(avih, strl(strh, strf)) + movi."""
    avih = struct.pack("<10I", us, 0, 0, 0, len(frames), 0, 1, 0, w, h) + b"\0" * 16
    strh = (b"vids" + fourcc + struct.pack("<IHHIIIIIIII", 0, 0, 0, 0, 1, us and 1,
                                           0, len(frames), 0, 0, 0)
            + b"\0" * 8)
    strf = struct.pack("<IiiHH4sIiiII", 40, w, h, 1, bits, fourcc,
                       w * h * bits // 8, 0, 0, 0, 0)
    strl = _chunk(b"LIST", b"strl" + _chunk(b"strh", strh) + _chunk(b"strf", strf))
    hdrl = _chunk(b"LIST", b"hdrl" + _chunk(b"avih", avih) + strl)
    movi = _chunk(b"LIST", b"movi" + b"".join(_chunk(b"00db", f) for f in frames))
    body = b"AVI " + hdrl + movi
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def video_eye_frame(h: int, w: int, cx: int, cy: int, r: int) -> np.ndarray:
    """A dark disc on a bright field, with a glint — the mock's shape."""
    Y, X = np.ogrid[:h, :w]
    f = np.full((h, w), 190, np.uint8)
    f[(X - cx) ** 2 + (Y - cy) ** 2 < r * r] = 20
    f[(X - cx - r // 3) ** 2 + (Y - cy) ** 2 < max(2, r // 6) ** 2] = 250
    return f


class DiscTracking:
    """Stands in for EyeLoop (needs cv2 and a clone): the dark pixels under
    `track_threshold` inside the region are the pupil."""

    error = None
    available = True

    def track(self, frame, st):
        from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
        x0, y0, x1, y1 = st.crop_box(frame.shape)
        ys, xs = np.nonzero(frame[y0:y1, x0:x1] < st.track_threshold)
        if xs.size < 20:
            return None
        rad = float(np.sqrt(xs.size / np.pi))
        return PupilFit(xs.mean() + x0, ys.mean() + y0, rad, rad, 0.0)


def face_frame(h=300, w=420, cx=210, cy=150, r=30, pupil=20, iris=26,
               glint=True, seed=0) -> np.ndarray:
    """A dim eye like the rig's: pupil a few levels under the iris, an eye
    opening, bright fur around, sensor noise and a reflection at the rim."""
    rng = np.random.default_rng(seed)
    Y, X = np.ogrid[:h, :w]
    f = np.full((h, w), 120.0)
    f[((X - cx) / (2.6 * r)) ** 2 + ((Y - cy) / (1.6 * r)) ** 2 < 1] = iris
    f[(X - cx) ** 2 + (Y - cy) ** 2 < r * r] = pupil
    if glint:
        f[(X - cx + int(r * 0.8)) ** 2 + (Y - cy - int(r * 0.8)) ** 2 < 16] = 235
    f += rng.normal(0, 1.0, f.shape)
    return np.clip(f, 0, 255).astype(np.uint8)
