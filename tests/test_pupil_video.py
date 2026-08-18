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
  * `smooth_sigma` and `min_confidence` reach the tracker from the panel, which
    is the whole reason they were added: their old hardcoded values make real
    frames untrackable.

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
    from acqApp.devices.pupil_cam.track_worker import TRACK_KEYS, track_params

    r.check("smooth_sigma" in TRACK_KEYS and "min_confidence" in TRACK_KEYS,
            "the two knobs real footage needs are panel-owned (TRACK_KEYS)")
    s = PupilSettings(smooth_sigma=12.0, min_confidence=0.04)
    tp = track_params(s)
    r.check(tp["smooth_sigma"] == 12.0 and tp["min_confidence"] == 0.04,
            "track_params carries them through to the tracker")
    from acqApp.devices.pupil_cam.tracking import PupilTracker
    t = PupilTracker(**tp)
    r.check(t.smooth_sigma == 12.0 and t.min_confidence == 0.04,
            "PupilTracker accepts them as configured, not as defaults")
    # CONTROL: the override above is not a no-op — the defaults differ from it.
    d = PupilSettings()
    r.check((d.smooth_sigma, d.min_confidence) != (12.0, 0.04),
            f"control: the defaults ({d.smooth_sigma}, {d.min_confidence}) are "
            f"not the overridden values")
    r.check(d.edge_select == "first",
            f"the default edge choice is the innermost edge, not the strongest "
            f"(got {d.edge_select!r})")

    # ── 5. the eyelid lock, which is why edge_select exists ──────────────────
    # The rig's own footage, in miniature: a low-contrast pupil edge (25→55)
    # with a far higher-contrast orbit→fur margin outside it (30→240). That is
    # the geometry "strongest" cannot survive — it takes the outer step on every
    # ray — and the whole reason the default is now the innermost edge.
    from acqApp.devices.pupil_cam.tracking import find_circular_edge

    # LR sits *inside* the annulus the rays sweep — as the eyelid does on the
    # rig clip. Put it outside and the frame proves nothing: both modes then
    # find the pupil simply because the lid was never a candidate.
    HH, WW, PCX, PCY, PR, LR = 300, 300, 150.0, 150.0, 40.0, 62.0
    Y, X = np.mgrid[0:HH, 0:WW].astype(np.float32)
    d = np.hypot(X - PCX, Y - PCY)
    lid = np.full((HH, WW), 240.0)          # saturated fur
    lid = np.where(d < LR, 55.0, lid)       # dark orbit inside the lid margin
    lid = np.where(d < PR, 25.0, lid)       # the pupil, only 30 levels darker
    g = np.hypot(X - (PCX + 0.35 * PR), Y - (PCY - 0.3 * PR))
    lid = np.where(g < 0.16 * PR, 250.0, lid)     # corneal glint
    frame = np.clip(lid + np.random.default_rng(1).normal(0, 2.0, lid.shape),
                    0, 255).astype(np.uint8)

    got = {}
    for sel in ("first", "strongest"):
        res = find_circular_edge(frame, (PCX + 3, PCY - 2), PR * 0.45, PR * 1.9,
                                 n_rays=64, edge_select=sel, refine_iters=3)
        got[sel] = res.radius
    r.check(got["first"] is not None and abs(got["first"] - PR) < 3.0,
            f"edge_select='first' finds the pupil at r={PR:.0f} "
            f"(got {got['first']})")
    # CONTROL: the old behaviour really does fail here, so the check above is
    # measuring the fix and not a frame that was easy all along.
    r.check(got["strongest"] is None or abs(got["strongest"] - PR) > 15.0,
            f"control: 'strongest' locks the lid margin instead "
            f"(got {got['strongest']}, lid at r={LR:.0f})")

    # ── 6. the temporal filter, and what a blink must still do ───────────────
    # Smoothing exists because a half-closed lid throws the fit off for one or
    # two frames and it returns — on rig footage that was a 22 px centre jump.
    # It must NOT hide a blink: those frames stay lost, and the estimate the
    # tracker resumes from is the one it held before the eye shut.
    # The odd frame has to stay inside the search band (r*(1±0.55) around the
    # seed) or the tracker legitimately loses it, and a lost frame is not what
    # this section is about — the excursions smoothing exists for are ones the
    # fit accepts.
    STEADY, ODD = 30.0, 38.0
    good = eye_frame(240, 320, 160, 120, int(STEADY))
    odd = eye_frame(240, 320, 160, 120, int(ODD))
    shut = np.full((240, 320), 190, np.uint8)        # no pupil at all

    def sequence(frames, **kw):
        t = PupilTracker(min_r=10, max_r=80, **kw)
        t.seed(160, 120, STEADY)
        return t, [t.process(f) for f in frames]

    seq = [good] * 6 + [odd] + [good] * 6
    _, sm = sequence(seq, smooth_median=3, smooth_ema=0.5)
    _, raw = sequence(seq, smooth_median=1, smooth_ema=1.0)
    sm_r = np.array([x.radius for x in sm], float)
    raw_r = np.array([x.radius for x in raw], float)
    r.check(abs(raw_r[6] - ODD) < 3.0,
            f"control: unsmoothed, the odd frame is reported in full "
            f"({raw_r[6]:.1f} of {ODD})")
    r.check(abs(sm_r[6] - STEADY) < 0.35 * (ODD - STEADY),
            f"the median absorbs a one-frame excursion "
            f"({sm_r[6]:.1f} vs raw {raw_r[6]:.1f}, steady {STEADY})")
    r.check(sm[6].raw_radius is not None
            and abs(sm[6].raw_radius - ODD) < 3.0,
            f"…and the unfiltered observation survives in raw_radius "
            f"({sm[6].raw_radius})")
    jit_sm = np.nanmax(np.abs(np.diff(sm_r)))
    jit_raw = np.nanmax(np.abs(np.diff(raw_r)))
    r.check(jit_sm < 0.5 * jit_raw,
            f"worst frame-to-frame step shrinks ({jit_sm:.2f} vs {jit_raw:.2f} px)")

    # A blink: four frames with no pupil, then the eye reopens.
    t, blink = sequence([good] * 5 + [shut] * 4 + [good] * 4,
                        smooth_median=3, smooth_ema=0.5)
    lost = [i for i, x in enumerate(blink) if not x.found]
    r.check(lost == [5, 6, 7, 8],
            f"the blink registers as lost frames, not interpolated over "
            f"(lost {lost}, expected [5, 6, 7, 8])")
    r.check(t.locked,
            "the annulus seed is retained through the blink (still locked)")
    r.check(blink[9].found and abs(blink[9].radius - STEADY) < 2.0,
            f"the first frame after the blink is right immediately, not crawled "
            f"back to ({blink[9].radius})")
    # CONTROL: the filter must not carry pre-blink values across the gap — if it
    # did, the frames above would report a radius instead of None.
    r.check(all(x.radius is None for x in blink[5:9]),
            "control: no smoothed value leaks into the lost frames")

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
        m.panel = type("P", (), {"settings": PupilSettings(video_path=video,
                                                           fps=30.0)})()
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
    d = PupilSettings()
    r.check(md.get("pupil_smooth_sigma") == d.smooth_sigma
            and md.get("pupil_min_confidence") == d.min_confidence
            and md.get("pupil_edge_select") == d.edge_select,
            f"metadata: the three new tracking parameters are filed too "
            f"({md.get('pupil_edge_select')}, {md.get('pupil_smooth_sigma')}, "
            f"{md.get('pupil_min_confidence')})")
    m.stop()
    # CONTROL: a live session files an empty string, not a stale path.
    m, _ = built("", True)
    r.check(m.metadata().get("pupil_video") == "",
            "control: a camera session files pupil_video as empty")
    m.stop()

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
