r"""
Pupil tracker validation against synthetic eyes with known ground truth.

  ..\..\.venv\Scripts\python.exe pupil_cam\_test_tracking.py

No hardware, no Qt — pure numpy, runs in a couple of seconds. Run it after any
change to tracking.py: the fitter is easy to break in ways that still produce
a plausible-looking circle on screen, and only ground truth catches that.

Every case prints its actual error, so a regression shows up as a number moving
rather than just a red line. Current expectation is sub-pixel everywhere.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

# Diagnostic prints in the device modules use characters a non-UTF-8 console
# cannot encode; unguarded, that raises inside the acquisition thread and
# looks like a device failure. See acqApp/console.py.
from acqApp.console import enable_safe_console
enable_safe_console()

import numpy as np

from acqApp.devices.pupil_cam.tracking import (
    coarse_seed, detect, fit_circle_robust, fit_circle_taubin, fit_ellipse,
    PupilTracker,
)

rng = np.random.default_rng(0)
H, W = 240, 320


def eye(cx, cy, r, *, noise=3.0, glint=True, eyelid=0.0, iris=150, pupil=25,
        ellipse=None):
    """Synthetic eye: dark pupil on a brighter iris, + IR glint + eyelid."""
    Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
    if ellipse is None:
        d = np.hypot(X - cx, Y - cy) / r
    else:
        a, b, th = ellipse
        t = np.radians(th)
        u = (X - cx) * np.cos(t) + (Y - cy) * np.sin(t)
        v = -(X - cx) * np.sin(t) + (Y - cy) * np.cos(t)
        d = np.hypot(u / a, v / b)
    img = np.where(d < 1.0, float(pupil), float(iris))
    if glint:                                   # corneal reflection in the pupil
        g = np.hypot(X - (cx + 0.35 * r), Y - (cy - 0.3 * r))
        img = np.where(g < 0.16 * r, 245.0, img)
    if eyelid > 0:                              # dark lid covering the top
        img = np.where(Y < cy - r * (1.0 - eyelid), 40.0, img)
    return np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)


results: list[bool] = []


def check(name, errs, r_errs, misses, n, tol=2.0):
    errs, r_errs = np.asarray(errs), np.asarray(r_errs)
    ok = misses == 0 and errs.max() < tol and np.abs(r_errs).max() < tol
    print(f"{'PASS' if ok else 'FAIL'} {name:34s} "
          f"centre max={errs.max():5.2f} px | "
          f"radius mean={r_errs.mean():+5.2f} max={np.abs(r_errs).max():5.2f} px | "
          f"misses {misses}/{n}")
    results.append(ok)


def check_bool(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name:34s} {detail}")
    results.append(ok)


# ── 1. clean disc across the radius range ────────────────────────────────────
errs, r_errs, misses = [], [], 0
for r in range(12, 75, 3):
    res = detect(eye(160.0, 120.0, float(r)), 60, 10, 80)
    if res.radius is None:
        misses += 1
        continue
    errs.append(np.hypot(res.center_x - 160.0, res.center_y - 120.0))
    r_errs.append(res.radius - r)
check("clean disc, r=12..72", errs, r_errs, misses, 21)

# ── 2. sub-pixel accuracy at random poses ────────────────────────────────────
errs, r_errs, misses = [], [], 0
for _ in range(30):
    cx, cy = rng.uniform(120, 200), rng.uniform(90, 150)
    r = rng.uniform(18, 55)
    res = detect(eye(cx, cy, r), 60, 10, 80)
    if res.radius is None:
        misses += 1
        continue
    errs.append(np.hypot(res.center_x - cx, res.center_y - cy))
    r_errs.append(res.radius - r)
check("random pose, sub-pixel", errs, r_errs, misses, 30)

# ── 3. eyelid occlusion with the annulus already seeded ──────────────────────
# The steady state: the tracker or the operator has placed the annulus, so this
# asks only whether the ray/fit stage survives a partial arc. The seed is
# deliberately offset from truth, so re-centring is exercised too.
for lid in (0.2, 0.4, 0.6):
    errs, r_errs, misses = [], [], 0
    for _ in range(10):
        cx, cy, r = rng.uniform(140, 180), rng.uniform(100, 140), rng.uniform(25, 45)
        seed = (cx + rng.uniform(-4, 4), cy + rng.uniform(-4, 4),
                r * rng.uniform(0.8, 1.2))
        res = detect(eye(cx, cy, r, eyelid=lid), 60, 10, 80, seed=seed)
        if res.radius is None:
            misses += 1
            continue
        errs.append(np.hypot(res.center_x - cx, res.center_y - cy))
        r_errs.append(res.radius - r)
    check(f"eyelid {lid:.0%}, seeded annulus", errs or [9], r_errs or [9],
          misses, 10)

# ── 4. auto-seed defeated → must fail loudly, never confidently wrong ────────
# A dark eyelid band that merges with the pupil and outweighs it is a single
# connected blob, so no "largest/roundest component" rule can recover the
# pupil. The contract is no detection, not a confident wrong radius.
bad = safe = 0
for lid in (0.2, 0.4, 0.6):
    for _ in range(6):
        cx, cy, r = rng.uniform(140, 180), rng.uniform(100, 140), rng.uniform(25, 45)
        res = detect(eye(cx, cy, r, eyelid=lid), 60, 10, 80)
        if res.radius is None or abs(res.radius - r) < 2.0:
            safe += 1
        else:
            bad += 1
check_bool("auto-seed defeated: no false lock", bad == 0,
           f"({safe} safe, {bad} confidently wrong)")

# ── 5. noise ─────────────────────────────────────────────────────────────────
errs, r_errs, misses = [], [], 0
for nz in (5, 10, 15, 20):
    for _ in range(5):
        cx, cy, r = rng.uniform(140, 180), rng.uniform(100, 140), rng.uniform(25, 45)
        res = detect(eye(cx, cy, r, noise=nz), 60, 10, 80)
        if res.radius is None:
            misses += 1
            continue
        errs.append(np.hypot(res.center_x - cx, res.center_y - cy))
        r_errs.append(res.radius - r)
check("noise sigma 5..20", errs, r_errs, misses, 20)

# ── 6. ellipse mode on an off-axis pupil ─────────────────────────────────────
errs, r_errs, misses = [], [], 0
for _ in range(15):
    cx, cy = rng.uniform(140, 180), rng.uniform(100, 140)
    a, b, th = rng.uniform(30, 45), rng.uniform(18, 28), rng.uniform(0, 180)
    res = detect(eye(cx, cy, (a + b) / 2, ellipse=(a, b, th)), 60, 10, 80,
                 fit="ellipse")
    if res.radius is None:
        misses += 1
        continue
    errs.append(np.hypot(res.center_x - cx, res.center_y - cy))
    r_errs.append(res.radius - (a + b) / 2)
check("ellipse fit, off-axis", errs, r_errs, misses, 15)

# ── 7. PupilTracker over a dilation sequence with a blink ────────────────────
trk = PupilTracker()
errs, r_errs, misses, recovered = [], [], 0, None
for i in range(120):
    t = i / 30.0
    r = 35 + 15 * np.sin(2 * np.pi * 0.1 * t)
    cx, cy = 160 + 3 * np.sin(t), 120 + 2 * np.cos(t)
    blink = 55 <= i < 62
    res = trk.process(eye(cx, cy, r, eyelid=0.95 if blink else 0.0))
    if blink:
        continue
    if res.radius is None:
        misses += 1
        continue
    if i > 62 and recovered is None:
        recovered = i - 62
    errs.append(np.hypot(res.center_x - cx, res.center_y - cy))
    r_errs.append(res.radius - r)
check("tracker, dilation + blink", errs, r_errs, misses, 112)
print(f"     recovered {recovered} frame(s) after the blink")

# ── 8. a frame with no pupil must not hallucinate one ────────────────────────
blank = np.clip(np.full((H, W), 150.0) + rng.normal(0, 3, (H, W)),
                0, 255).astype(np.uint8)
res = detect(blank, 60, 10, 80)
check_bool("blank frame: no detection", res.radius is None,
           f"(radius={res.radius}, conf={res.confidence:.2f})")

# ── 9. bright-pupil / retro-illumination ─────────────────────────────────────
res = detect(eye(160, 120, 30, iris=40, pupil=200, glint=False),
             100, 10, 80, polarity="falling")
check_bool("bright pupil, polarity=falling",
           res.radius is not None and abs(res.radius - 30) < 2,
           f"r={res.radius:.2f} (truth 30)" if res.radius else "no detection")

# ── 10. primitives ───────────────────────────────────────────────────────────
th = np.linspace(0, 2 * np.pi, 40, endpoint=False)
x, y = 50 + 20 * np.cos(th), 30 + 20 * np.sin(th)
c = fit_circle_taubin(x, y)
check_bool("Taubin fit, exact circle",
           c is not None and abs(c[0] - 50) < 1e-6 and abs(c[2] - 20) < 1e-6,
           f"-> {tuple(round(v, 4) for v in c)}")

xo = x.copy()
xo[:6] += 25                                   # simulated eyelash outliers
rob = fit_circle_robust(xo, y)
check_bool("robust fit rejects 6 outliers",
           rob is not None and abs(rob[2] - 20) < 0.5 and rob[3].sum() == 34,
           f"-> c=({rob[0]:.2f},{rob[1]:.2f}) r={rob[2]:.3f} "
           f"inliers={rob[3].sum()}/40")

worst = 0.0
for thd in range(0, 180, 15):
    tt = np.linspace(0, 2 * np.pi, 64, endpoint=False)
    ca, sa = np.cos(np.radians(thd)), np.sin(np.radians(thd))
    ex = 100 + 40 * np.cos(tt) * ca - 20 * np.sin(tt) * sa
    ey = 100 + 40 * np.cos(tt) * sa + 20 * np.sin(tt) * ca
    e = fit_ellipse(ex, ey)
    worst = max(worst, abs((e[4] - thd + 90) % 180 - 90))
check_bool("ellipse angle, 0..165 deg", worst < 0.01,
           f"worst error {worst:.4f} deg")

seed = coarse_seed(eye(160, 120, 30), 60, 10, 80)
check_bool("coarse seed lands in the pupil",
           seed is not None and np.hypot(seed[0] - 160, seed[1] - 120) < 5,
           f"-> {tuple(round(v, 1) for v in seed)} (truth 160, 120, ~30)")

print("\n" + ("ALL CHECKS PASSED" if all(results)
              else f"{results.count(False)}/{len(results)} CHECKS FAILED"))
sys.exit(0 if all(results) else 1)
