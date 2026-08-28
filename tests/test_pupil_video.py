"""
Replaying recorded footage as the pupil camera.

The tracker had only ever been tested against synthetic eyes
(`devices/pupil_cam/_test_tracking.py`) and against the mock's clean disc. Real
footage is what shows whether a parameter set survives fur, lashes and a glint,
so `video.py` makes a clip a third frame source — and this holds the parts of
that which can be checked without any particular file existing:

  * the AVI reader gets the right pixels out of each supported layout, built
    here byte by byte rather than depending on a clip on E:;
  * a compressed FourCC fails with a message naming it, since there is no
    decoder in this venv to fall back on;
  * `VideoFileCameraWorker` publishes real frames through the real `PullWorker`
    machinery, loops, and honours the requested rate;
  * the adapter picks it over both twins when `video_path` is set, falls back
    rather than dying when the file is unusable, and records which clip in the
    metadata — a session replayed from footage must never read as rig data;

The tracker itself was archived on 2026-08-24 (PLAN §7 (ai)); what is left is
the frame source, which the pupil camera still uses.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pupil_video.py
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

from _harness import Report, isolate_user_state, pump, qt_app


# ── building an AVI, so the test owns its own input ──────────────────────────

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


def i420(y: np.ndarray) -> bytes:
    """Y plane plus mid-grey 4:2:0 chroma, which the reader must ignore."""
    h, w = y.shape
    uv = np.full((h // 2, w // 2), 128, np.uint8)
    return y.tobytes() + uv.tobytes() + uv.tobytes()


def dib_rows(img: np.ndarray, px: int) -> bytes:
    """`img` as DIB scanlines — each padded up to a 4-byte boundary, as the
    format requires. `write_avi` alone would pack them, which is the one case a
    reader that ignores the stride still gets right."""
    h, w = img.shape[:2]
    stride = ((w * px + 3) // 4) * 4
    row = img.reshape(h, w * px) if img.ndim == 3 else img
    pad = b"\0" * (stride - w * px)
    return b"".join(bytes(row[y]) + pad for y in range(h))


def eye_frame(h: int, w: int, cx: int, cy: int, r: int) -> np.ndarray:
    """A dark disc on a bright field, with a glint — the mock's shape."""
    Y, X = np.ogrid[:h, :w]
    f = np.full((h, w), 190, np.uint8)
    f[(X - cx) ** 2 + (Y - cy) ** 2 < r * r] = 20
    f[(X - cx - r // 3) ** 2 + (Y - cy) ** 2 < max(2, r // 6) ** 2] = 250
    return f


def main() -> int:  # noqa: PLR0915 — one linear scenario, split only by section
    r = Report("pupil-video")
    tmp = Path(tempfile.mkdtemp(prefix="pupil_video_"))
    H, W = 64, 96

    from acqApp.devices.pupil_cam.avi import AviReader

    # ── 1. the reader, per layout ────────────────────────────────────────────
    y0, y1 = eye_frame(H, W, 40, 30, 12), eye_frame(H, W, 52, 34, 15)

    p = write_avi(tmp / "planar.avi", [i420(y0), i420(y1)], W, H, b"IYUV", 24)
    rd = AviReader(p)
    r.check((rd.width, rd.height) == (W, H),
            f"IYUV: geometry read from strf ({rd.width}x{rd.height})")
    r.check(len(rd) == 2, f"IYUV: both frames indexed (got {len(rd)})")
    r.check(np.array_equal(rd.luma(0), y0) and np.array_equal(rd.luma(1), y1),
            "IYUV: the Y plane comes back exactly, chroma ignored")
    r.check(abs(rd.fps - 20.0) < 1e-6, f"IYUV: fps from avih ({rd.fps:.2f})")
    # CONTROL: the frames genuinely differ, so "comes back exactly" is not
    # satisfied by returning the same buffer twice.
    r.check(not np.array_equal(y0, y1),
            "control: the two source frames are not identical")

    p = write_avi(tmp / "gray.avi", [y0.tobytes(), y1.tobytes()], W, H,
                  b"Y800", 8)
    rd = AviReader(p)
    r.check(np.array_equal(rd.luma(0), y0),
            "Y800: 8-bit luma passes through unflipped")

    # BI_RGB is bottom-up, so a correct reader must flip it back.
    bgr = np.repeat(y0[:, :, None], 3, axis=2)[::-1]
    p = write_avi(tmp / "dib.avi", [bgr.tobytes()], W, H, b"\0\0\0\0", 24)
    rd = AviReader(p)
    got = rd.luma(0)
    r.check(got.shape == (H, W) and int(np.abs(got.astype(int)
                                               - y0.astype(int)).max()) <= 1,
            "BI_RGB 24-bit: flipped upright and converted to luma")
    # CONTROL: without the flip the top row would be the source's bottom row.
    r.check(not np.array_equal(got, bgr[:, :, 0]),
            "control: the BI_RGB flip actually happened")

    # A width whose row bytes are NOT 4-aligned. DIB pads every scanline up to
    # the boundary; a reader that assumes width*px shears the image a little
    # further on each row. W=96 above cannot show this — 96*3 is already
    # aligned — which is exactly how it went unnoticed.
    W2 = 97
    r.check((W2 * 3) % 4 != 0 and (W2 * 1) % 4 != 0,
            f"control: {W2}px rows are unaligned at both 8- and 24-bit, so "
            f"these two cases can actually fail")
    y2 = eye_frame(H, W2, 44, 30, 12)
    p = write_avi(tmp / "pad8.avi", [dib_rows(y2[::-1], 1)], W2, H,
                  b"\0\0\0\0", 8)
    r.check(np.array_equal(AviReader(p).luma(0), y2),
            "BI_RGB 8-bit: padded scanlines read back unsheared")
    bgr2 = np.repeat(y2[:, :, None], 3, axis=2)[::-1]
    p = write_avi(tmp / "pad24.avi", [dib_rows(bgr2, 3)], W2, H,
                  b"\0\0\0\0", 24)
    got2 = AviReader(p).luma(0)
    r.check(got2.shape == (H, W2) and int(np.abs(got2.astype(int)
                                                 - y2.astype(int)).max()) <= 1,
            "BI_RGB 24-bit: ditto, and still flipped upright")

    # ── 2. a compressed clip must say so, not guess ──────────────────────────
    p = write_avi(tmp / "mjpg.avi", [b"\xff\xd8" + b"\0" * (W * H)], W, H,
                  b"MJPG", 24)
    try:
        AviReader(p)
        r.check(False, "MJPG: raises rather than returning garbage")
    except ValueError as e:
        r.check("MJPG" in str(e) and "decoder" in str(e).lower(),
                f"MJPG: refused, naming the codec and the missing decoder")

    # ── 3. the worker ────────────────────────────────────────────────────────
    app = qt_app()                     # a real QThread needs a real app
    from acqApp.devices.pupil_cam.video import VideoFileCameraWorker

    clip = write_avi(tmp / "clip.avi", [i420(eye_frame(H, W, 30 + 4 * i, 30, 12))
                                        for i in range(5)], W, H, b"IYUV", 24)
    wk = VideoFileCameraWorker(clip, fps=60.0)
    r.check(wk.frame_shape == (H, W), f"worker: frame_shape {wk.frame_shape}")
    r.check(wk.n_frames == 5, f"worker: n_frames {wk.n_frames}")
    seen: list[np.ndarray] = []
    wk.set_sink(seen.append)           # the sink sees every frame
    wk.start()
    pump(app, 0.6)
    wk.stop()
    r.check(len(seen) > 5,
            f"worker: loops past the end of the clip ({len(seen)} frames of 5)")
    r.check(all(f.shape == (H, W) and f.dtype == np.uint8 for f in seen),
            "worker: every published frame is (H, W) uint8")
    r.check(np.array_equal(seen[0], seen[5]) if len(seen) > 5 else False,
            "worker: frame 5 is frame 0 again — the loop wraps in order")
    # A copy, not a memmap view: two frames must not alias one buffer.
    r.check(not np.shares_memory(seen[0], seen[1]),
            "worker: frames are copies, so the sink can keep them")

    wk2 = VideoFileCameraWorker(clip, fps=20.0, loop=False)
    wk2.start()
    pump(app, 0.8)
    r.check(wk2.isFinished() or not wk2.isRunning(),
            "worker: loop=False stops itself at the end of the clip")
    wk2.stop()

    # ── 4. the adapter's choice, and what lands in the file ──────────────────
    isolate_user_state()               # the panel persists on every edit
    from acqApp.devices.pupil_cam.acquisition import MockPupilCameraWorker
    from acqApp.devices.pupil_cam.settings import PupilSettings

    from acqApp.adapters.pupil_cam import PupilCamModule

    class FakeWin:
        """Only what build_session touches."""
        def __init__(self) -> None:
            self.messages: list[str] = []

        def status(self, msg: str) -> None:
            self.messages.append(msg)

        def on_worker_error(self, _msg) -> None:
            pass

    def built(video: str, emulate: bool):
        win = FakeWin()
        m = PupilCamModule(win)
        m.panel = type("P", (), {
            "settings": PupilSettings(video_path=video, fps=30.0),
            "set_measured_rate": lambda self, *a, **kw: None,
        })()
        m.build_session(emulate)
        return m, win

    m, _ = built(str(clip), True)
    r.check(isinstance(m.worker, VideoFileCameraWorker),
            "adapter: video_path wins over the mock in emulate mode")
    m.stop()

    m, _ = built("", True)
    r.check(isinstance(m.worker, MockPupilCameraWorker),
            "control: no video_path still gives the mock")
    m.stop()

    m, win = built(str(tmp / "nope.avi"), True)
    r.check(isinstance(m.worker, MockPupilCameraWorker),
            "adapter: a missing clip falls back instead of killing the session")
    r.check(any("video" in x for x in win.messages),
            f"adapter: and says so in the status bar ({win.messages})")
    m.stop()

    m, _ = built(str(clip), True)
    md = m.metadata()
    r.check(md.get("pupil_video") == str(clip),
            "metadata: the clip is recorded, so replayed data cannot pass as rig data")
    r.check(md.get("pupil_fps") == 30.0 and "pupil_edge_select" not in md,
            f"metadata: camera settings are filed and the archived tracking "
            f"parameters are not ({sorted(md)})")
    m.stop()
    # CONTROL: a live session files an empty string, not a stale path.
    m, _ = built("", True)
    r.check(m.metadata().get("pupil_video") == "",
            "control: a camera session files pupil_video as empty")
    m.stop()

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
