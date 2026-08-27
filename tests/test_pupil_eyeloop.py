"""The EyeLoop pupil tracker as acqApp drives it — the seam, and its silences.

Everything EyeLoop does wrong here does it *quietly*: a failed frame returns
the previous frame's fit, a fit that has walked into the eyelid still returns a
plausible ellipse, and a bad settings type makes every frame throw inside a
bare `except` with nothing logged that resembles the mistake. So each check
below is paired with a control that fails the property being claimed —
otherwise "it tracked 151 frames" is a sentence about nothing.

The rig clips on `E:` are the real evidence; when they are absent (any machine
but this one) the clip-based checks are skipped and the synthetic half still
runs. Skips are counted and printed, never passed off as passes.

Needs a patched EyeLoop clone (docs/EYELOOP.md). Without one the whole file
skips: an absent clone must leave the pupil camera working, and that is itself
one of the checks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import Report, isolate_user_state          # noqa: E402

from acqApp.devices.pupil_cam.settings import PupilSettings   # noqa: E402
from acqApp.devices.pupil_cam.tracking import PupilTracking   # noqa: E402

CLIPS = {
    "pAce": (Path(r"E:\pAce\VF203.2R\20260701\FOV1_T1\FOV1_T1_Pupil.avi"), (850, 490)),
    "State": (Path(r"E:\State\VF182.6B\20260709\FOV1_T1\FOV1_T1_Pupil.avi"), (900, 490)),
}


def synthetic_eye(w=400, h=400, centre=(200, 200), radius=55, glint=None):
    """A dark disc on mid-grey, optionally with a saturated reflection in it."""
    img = np.full((h, w), 150, np.uint8)
    yy, xx = np.ogrid[:h, :w]
    img[(xx - centre[0]) ** 2 + (yy - centre[1]) ** 2 <= radius ** 2] = 20
    if glint is not None:
        gx, gy, gr = glint
        img[(xx - gx) ** 2 + (yy - gy) ** 2 <= gr ** 2] = 235
    return img


def frame_with_eye(eye, at=(850, 490), shape=(1208, 1928)):
    """Drop a synthetic eye into a full-size dark frame at `at`."""
    full = np.full(shape, 12, np.uint8)
    h, w = eye.shape
    x0, y0 = int(at[0] - w // 2), int(at[1] - h // 2)
    full[y0:y0 + h, x0:x0 + w] = eye
    return full


def main() -> int:
    isolate_user_state()
    r = Report("pupil-eyeloop")

    try:
        from acqApp.devices.pupil_cam.eyeloop_tracker import (
            EYELOOP_DIR, GlintRemoval, Pin, PupilFit, remove_glints)
    except ImportError as e:
        print(f"[pupil-eyeloop] cannot import the wrapper: {e}")
        return 1

    have_clone = (EYELOOP_DIR / "eyeloop").is_dir()

    # ── the contract that must hold with NO clone ────────────────────────────
    # A rig that has never set EyeLoop up must run exactly as it did before.
    st_off = PupilSettings(track=False, limit_x=200, limit_y=200, limit_r=150)
    pt_off = PupilTracking()
    r.check(pt_off.track(synthetic_eye(), st_off) is None,
            "tracking off returns None regardless of anything else")

    st_noregion = PupilSettings(track=True, limit_r=0.0)
    r.check(PupilTracking().track(synthetic_eye(), st_noregion) is None,
            "no eye region means no tracking (the crop is not optional)")

    if not have_clone:
        print(f"[pupil-eyeloop] no EyeLoop clone at {EYELOOP_DIR} — "
              f"skipping the tracking checks (see docs/EYELOOP.md)")
        return r.finish()

    # ── it tracks, and the crop is what makes it work ───────────────────────
    eye = synthetic_eye(glint=(180, 215, 9))
    full = frame_with_eye(eye)
    st = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                       limit_x=850, limit_y=490, limit_r=200)

    pt = PupilTracking()
    fit = pt.track(full, st)
    r.check(fit is not None, "a synthetic eye is tracked through the app path")
    if fit is not None:
        r.check(abs(fit.center_x - 850) < 12 and abs(fit.center_y - 490) < 12,
                f"the fit is in FULL-FRAME pixels ({fit.center_x:.0f},"
                f"{fit.center_y:.0f}) not crop pixels")
        r.check(abs(fit.radius - 55) < 8,
                f"radius {fit.radius:.1f} recovers the synthetic 55")
        r.check(fit.axis_ratio > 0.9,
                f"a round pupil fits round (ratio {fit.axis_ratio:.2f})")

    # CONTROL: the same eye with no region to crop to — a full rig frame is
    # what EyeLoop cannot fit, and it is why the eye region is mandatory.
    st_full = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                            limit_x=964, limit_y=604, limit_r=600)
    ctl = PupilTracking().track(full, st_full)
    r.check(ctl is None or abs(ctl.radius - 55) > 15,
            "control: a region covering the whole frame does NOT recover it")

    # ── a failure must read as a failure, not as the last good frame ────────
    pt2 = PupilTracking()
    good = pt2.track(full, st)
    rng = np.random.default_rng(0)
    noise = frame_with_eye(rng.integers(90, 150, (400, 400), dtype=np.uint8))
    misses = [pt2.track(noise, st) for _ in range(8)]
    r.check(good is not None and all(m is None for m in misses),
            "a frame with no pupil returns None, not the previous fit")
    # CONTROL: EyeLoop's own state still holds that stale answer, which is
    # exactly the bug being defended against.
    r.check(pt2._tracker is not None
            and pt2._tracker._shape.fit_model.params is None,
            "control: params really was nulled, not merely re-read")

    # ── settings changes must not silently kill every frame ─────────────────
    # Floats in the walk radius make np.clip raise inside a bare except.
    st_moved = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                             limit_x=850, limit_y=490, limit_r=120)
    pt3 = PupilTracking()
    pt3.track(full, st)
    box_a = pt3._box
    pt3.track(full, st_moved)
    r.check(box_a != pt3._box, "resizing the eye region re-arms the tracker")
    r.check(pt3.track(full, st_moved) is not None,
            "and it still tracks after the re-arm")

    # ── reflection removal: it removes, and it stays inside the pupil ────────
    glinty = synthetic_eye(glint=(180, 215, 9))
    cfg = GlintRemoval(enabled=True, threshold=120, pad=4, ring=6,
                       search_scale=0.85)
    cleaned, mask = remove_glints(glinty, (200, 200), 55, cfg, (55, 55, 0))
    r.check(mask.sum() > 0, "the reflection is found")
    r.check(cleaned[215, 180] < 120,
            f"and blanked to {cleaned[215, 180]} — it reads as pupil again")
    # CONTROL: the mask must not reach the pupil boundary. Masking the rim is
    # what erases the edge and inflates the radius (+3.5 px, docs/EYELOOP.md).
    yy, xx = np.ogrid[:400, :400]
    d = np.hypot(xx - 200, yy - 200)
    r.check(not mask[d > 55 * 0.95].any(),
            "control: nothing outside 0.95 r is masked")

    # a pin reaches what the automatic pass will not
    far = synthetic_eye(glint=(200, 250, 10))     # at ~0.91 r, outside reach
    tight = GlintRemoval(enabled=True, threshold=120, search_scale=0.55)
    _, m_auto = remove_glints(far, (200, 200), 55, tight, (55, 55, 0))
    _, m_pin = remove_glints(far, (200, 200), 55,
                             GlintRemoval(enabled=True, threshold=120,
                                          search_scale=0.55,
                                          pins=(Pin(200, 250, 14),)),
                             (55, 55, 0))
    r.check(m_auto.sum() == 0, "control: the automatic pass cannot reach it")
    r.check(m_pin.sum() > 0, "a pin can — pins are exempt from reach")

    # pins are stored in full-frame pixels and must survive a region move
    st_pin = PupilSettings(track=True, track_threshold=60, limit_x=850,
                           limit_y=490, limit_r=200,
                           cr_pins=[(830.0, 505.0, 12.0)])
    box = st_pin.crop_box(full.shape)
    r.check(box == (650, 290, 1050, 690), f"crop box from the circle: {box}")
    pt4 = PupilTracking()
    r.check(pt4.track(full, st_pin) is not None,
            "a pinned reflection does not break tracking")

    # ── the rig clips, where the numbers came from ──────────────────────────
    from acqApp.devices.pupil_cam.avi import AviReader
    for name, (path, (ex, ey)) in CLIPS.items():
        if not path.exists():
            r.note(f"SKIP {name}: clip not on this machine")
            continue
        rd = AviReader(str(path))
        s = PupilSettings(track=True, track_threshold=60, cr_remove=False,
                          limit_x=ex, limit_y=ey, limit_r=200)
        p = PupilTracking()
        fits = [p.track(rd.luma(i), s) for i in range(len(rd))]
        ok = [f for f in fits if f is not None]
        r.check(len(ok) == len(fits),
                f"{name}: {len(ok)}/{len(fits)} frames fit")
        rad = np.array([f.radius for f in ok])
        r.check(rad.std() < 4.0,
                f"{name}: radius is steady across the clip "
                f"({rad.mean():.1f} +- {rad.std():.2f} px)")

    return r.finish()


if __name__ == "__main__":
    raise SystemExit(main())
