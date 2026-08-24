"""Registering the DMD to the ORCA — patterns, decode, and the fit.

For aiming: an ROI is drawn in *camera* pixels, a mask is in *DMD mirrors*, and
turning one into the other needs a measured transform. Nothing here projects or
grabs; it is the pure half, so the whole pipeline can be tested against a known
transform before any light is emitted (PLAN.md §2).

Everything rests on **complementary pairs**: project a pattern and its inverse,
and `m = (on - off) / (on + off)` cancels the sample. Structure, background and
vignetting are identical in both frames and divide out; what survives is how
strongly the projector modulates each *camera* pixel. So `m` is both the
per-pixel bit decision and, thresholded, the DMD's field boundary.

Two ways to use it:

  `checkerboard_pair()`  one pair — the field extent and an eyeball check, with
                         corner marks that are mutually distinguishable so a
                         mirror flip cannot pass as a valid registration.
  `gray_planes()`        ~2*(10+10) frames Gray-coding each mirror's x and y, so
                         every camera pixel is labelled with the mirror that lit
                         it. Thousands of correspondences rather than four —
                         and four points fit a homography *exactly*, leaving no
                         residual to tell a good fit from a bad one.

Patterns are built at the device's own size and must be handed to
`AlpDevice.project()` **directly**. Do not route them through `build_frame`: its
scale/rotation/offset — and `fit`, which overrides all three — would transform
the very geometry this is measuring.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

ON, OFF = np.uint8(255), np.uint8(0)


class CalibrationError(RuntimeError):
    """The sweep could not be registered — with a reason worth reading."""



# Modulation below this counts as "the projector does not reach this pixel".
# Well under a real lit/unlit contrast, well over sensor noise on a dark frame.
MIN_MODULATION = 0.15


# ══════════════════════════════════════════════════════════════════════════════
#  Patterns
# ══════════════════════════════════════════════════════════════════════════════

def _blank(width: int, height: int) -> np.ndarray:
    return np.full((height, width), OFF, np.uint8)


def corner_marks(width: int, height: int, *, inset: int = 40,
                 dot: int = 12, gap: int = 26) -> np.ndarray:
    """Four corner marks carrying 1/2/3/4 dots, clockwise from top-left.

    Distinguishable, not merely present: the relay can introduce a mirror flip,
    and four identical marks cannot reveal one — a flipped field still shows a
    mark in every corner. Counting dots pins orientation and handedness.
    """
    f = _blank(width, height)
    corners = ((inset, inset, +1, +1), (width - inset, inset, -1, +1),
               (width - inset, height - inset, -1, -1),
               (inset, height - inset, +1, -1))
    for n, (cx, cy, sx, sy) in enumerate(corners, start=1):
        for i in range(n):
            x = int(cx + sx * i * gap)
            y = int(cy)
            y0, y1 = max(0, y - dot // 2), min(height, y + dot // 2 + 1)
            x0, x1 = max(0, x - dot // 2), min(width, x + dot // 2 + 1)
            f[y0:y1, x0:x1] = ON
    return f


def checkerboard(width: int, height: int, square: int = 64, *,
                 invert: bool = False, marks: bool = True) -> np.ndarray:
    """Checkerboard at the device size, optionally with the corner marks.

    The marks are ORed in on both phases, so they survive the differencing as a
    common feature rather than flipping with the board.
    """
    if square < 1:
        raise ValueError("square must be >= 1 px")
    y, x = np.ogrid[:height, :width]
    board = ((x // square + y // square) % 2).astype(bool)
    if invert:
        board = ~board
    f = np.where(board, ON, OFF).astype(np.uint8)
    if marks:
        f = np.maximum(f, corner_marks(width, height))
    return f


def checkerboard_pair(width: int, height: int, square: int = 64, *,
                      marks: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """(pattern, its inverse) — the one pair that gives the field extent."""
    return (checkerboard(width, height, square, invert=False, marks=marks),
            checkerboard(width, height, square, invert=True, marks=marks))


def _gray(v: np.ndarray) -> np.ndarray:
    """Binary → reflected Gray code."""
    return v ^ (v >> 1)


def gray_planes(width: int, height: int, *,
                step: int = 1) -> tuple[list[np.ndarray], int, int]:
    """Gray-coded bit planes for x then y → (planes, n_bits_x, n_bits_y).

    Gray rather than plain binary because adjacent mirrors differ in exactly one
    bit, so a pixel straddling a pattern boundary misdecodes by ±1 mirror rather
    than by half the panel.

    `step` is **mirrors per code step**, and it exists because the finest planes
    are the ones that fail. At the rig's measured 4.6 camera px per mirror a
    1-mirror stripe is 4.6 px, which any defocus washes out — and since `decode`
    requires EVERY plane, one unresolved plane invalidates every pixel in the
    frame and the sweep returns 0.0 % decoded (2026-08-24, a real run).
    Coarsening costs nothing that matters: the fit is a global model over
    thousands of points, so quantising each correspondence to `step` mirrors
    averages out, while an unresolved plane loses all of them.

    The caller converts codes back to mirrors — `decode` returns code units.

    Project each plane with its inverse (`255 - plane`); `decode` wants both.
    """
    step = max(1, int(step))
    nx, ny = (width + step - 1) // step, (height + step - 1) // step
    nbx = max(1, int(np.ceil(np.log2(max(nx, 2)))))
    nby = max(1, int(np.ceil(np.log2(max(ny, 2)))))
    gx = _gray(np.arange(width, dtype=np.int64) // step)
    gy = _gray(np.arange(height, dtype=np.int64) // step)

    planes: list[np.ndarray] = []
    for k in range(nbx - 1, -1, -1):                    # MSB first
        row = np.where((gx >> k) & 1, ON, OFF).astype(np.uint8)
        planes.append(np.broadcast_to(row, (height, width)).copy())
    for k in range(nby - 1, -1, -1):
        col = np.where((gy >> k) & 1, ON, OFF).astype(np.uint8)
        planes.append(np.broadcast_to(col[:, None], (height, width)).copy())
    return planes, nbx, nby


# ══════════════════════════════════════════════════════════════════════════════
#  Decode
# ══════════════════════════════════════════════════════════════════════════════

def modulation(on: np.ndarray, off: np.ndarray) -> np.ndarray:
    """(on - off) / (on + off), the sample-cancelled contrast per camera pixel."""
    a = np.asarray(on, dtype=np.float64)
    b = np.asarray(off, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"pair shapes differ: {a.shape} vs {b.shape}")
    return (a - b) / np.maximum(a + b, 1e-9)


def field_mask(on: np.ndarray, off: np.ndarray, *,
               min_modulation: float = MIN_MODULATION) -> np.ndarray:
    """Camera pixels the projector actually reaches, from one complementary pair.

    |m| rather than m: a pixel under a *dark* square of the first phase is lit in
    the second, and is every bit as much inside the field.
    """
    return np.abs(modulation(on, off)) >= min_modulation


def bounding_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the True region, end-exclusive; None if empty."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def decode(on_stack, off_stack, n_bits_x: int, n_bits_y: int, *,
           min_modulation: float = MIN_MODULATION):
    """Gray-coded stacks → (dmd_x, dmd_y, valid), all camera-shaped.

    `on_stack[i]`/`off_stack[i]` are the camera's view of plane i and its
    inverse, in `gray_planes` order. A pixel is valid only if *every* plane
    modulated it: one ambiguous bit is a wrong mirror coordinate, and a
    confidently wrong correspondence is worse than a missing one.
    """
    n = len(on_stack)
    if len(off_stack) != n:
        raise ValueError(f"stack lengths differ: {n} vs {len(off_stack)}")
    if n != n_bits_x + n_bits_y:
        raise ValueError(f"expected {n_bits_x + n_bits_y} planes, got {n}")

    # ONE PLANE AT A TIME, in float32. Materialising both stacks as float64 cost
    # 7.8 GB at ORCA full frame (40 frames x 10.5 Mpx, plus the temporaries of
    # `abs(m).min(axis=0)`) — enough to fail on the rig box, which already pins
    # GB of camera buffers. Gray decoding is a running XOR and the validity test
    # a running min, so nothing needs the whole stack at once. float32 is exact
    # for uint16 sums and differences; only the ratio rounds, ~1e-7, against
    # thresholds of 0 and 0.15.
    shape = np.asarray(on_stack[0]).shape
    worst = np.full(shape, np.inf, np.float32)      # min |m| over the planes
    x = np.zeros(shape, np.int64)
    y = np.zeros(shape, np.int64)
    run_x = run_y = None

    for i in range(n):
        a = np.asarray(on_stack[i], dtype=np.float32)
        b = np.asarray(off_stack[i], dtype=np.float32)
        if a.shape != shape or b.shape != shape:
            raise ValueError(f"plane {i} shape {a.shape}/{b.shape} != {shape}")
        m = (a - b) / np.maximum(a + b, np.float32(1e-9))
        np.minimum(worst, np.abs(m), out=worst)
        bit = m > 0.0
        # Gray → binary, MSB first: b[msb] = g[msb], then b[i] = b[i+1] ^ g[i].
        if i < n_bits_x:
            run_x = bit if run_x is None else (run_x ^ bit)
            x = (x << 1) | run_x
        else:
            run_y = bit if run_y is None else (run_y ^ bit)
            y = (y << 1) | run_y

    return x, y, worst >= min_modulation


def plane_coverage(on_stack, off_stack, *,
                   min_modulation: float = MIN_MODULATION) -> list[tuple]:
    """Per plane: (fraction it modulated, fraction still valid after it).

    The diagnostic for a decode that returned nothing, and the reason it reports
    a **running intersection** rather than per-plane numbers alone: `decode`
    requires every plane, so where the intersection collapses is the answer.

      collapse in the LAST few planes  → stripes finer than the optics resolve
      steady slide from the FIRST      → the field is not really modulating;
                                         what looked like a field was noise

    Those two have different fixes, which is why this is measured rather than
    guessed at.
    """
    worst = None
    out: list[tuple] = []
    for a, b in zip(on_stack, off_stack):
        a = np.asarray(a, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        m = np.abs((a - b) / np.maximum(a + b, np.float32(1e-9)))
        worst = m.copy() if worst is None else np.minimum(worst, m)
        out.append((float((m >= min_modulation).mean()),
                    float((worst >= min_modulation).mean())))
    return out


GRAY_STEPS = (1, 2, 4, 8, 16)       # candidates, coarsening


def finest_plane(width: int, height: int, axis: int, step: int) -> np.ndarray:
    """The LSB plane at `step` — the narrowest stripe the sweep would project."""
    planes, nbx, _nby = gray_planes(width, height, step=step)
    return planes[nbx - 1] if axis == 0 else planes[-1]


def resolve_gray_step(project: Callable[[np.ndarray], None],
                      grab: Callable[[], np.ndarray],
                      dmd_size: tuple[int, int], *,
                      field: float,
                      target: float = 0.75,
                      candidates=GRAY_STEPS,
                      min_modulation: float = MIN_MODULATION,
                      log: Callable[[str], None] = print) -> int:
    """Coarsen the Gray code until its finest stripe actually modulates.

    **Measured, not predicted.** Whether a 1-mirror stripe survives is a
    question about the relay's point spread; a px-per-mirror rule of thumb is a
    guess about it, and on 2026-08-24 the guess would have been wrong in both
    directions on two different rigs. Four exposures per candidate, against a
    sweep of forty — and a sweep that decodes 0.0 % costs all forty.

    `field` is the checkerboard's modulated fraction: the finest plane should
    modulate most of the same frame, so the test is relative to what the field
    already showed rather than to an absolute number. `target` is deliberately
    demanding — this tests only the FINEST plane, while `decode` intersects
    every plane, so a stripe that only just clears the bar still loses most of
    the frame to the intersection.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    want = target * float(field)
    best = candidates[-1]
    for step in candidates:
        cover = []
        for axis in (0, 1):
            p = finest_plane(w, h, axis, step)
            project(p)
            a = np.asarray(grab(), dtype=np.float32)
            project((255 - p).astype(np.uint8))
            b = np.asarray(grab(), dtype=np.float32)
            m = np.abs((a - b) / np.maximum(a + b, np.float32(1e-9)))
            cover.append(float((m >= min_modulation).mean()))
        log(f"[dmd-calib] finest stripe at {step:>2} mirror(s): "
            f"x {100 * cover[0]:5.1f}%, y {100 * cover[1]:5.1f}% of frame "
            f"(need {100 * want:.1f}%)")
        if min(cover) >= want:
            return step
        best = step
    log(f"[dmd-calib] no candidate resolved; using {best} and expecting trouble")
    return best


def _decode_diagnosis(cov: list[tuple], nbx: int, step: int,
                      scale: float | None) -> str:
    """Turn a coverage table into the sentence that names the fix."""
    if not cov:
        return "no planes were captured at all"
    if cov[0][0] < 0.02:
        return ("the FIRST plane modulates almost nothing, so this is not a "
                "resolution problem — the projector and camera are not seeing "
                "the same light. Check the illumination, the exposure, and "
                "that the sweep patterns really reach the sample")
    survives = [i for i, (_t, v) in enumerate(cov) if v >= 0.05]
    last = survives[-1] if survives else -1
    if last < 0:
        return ("coverage collapses immediately: individual planes modulate but "
                "not the SAME pixels, which is what noise looks like rather "
                "than a field")
    if last >= len(cov) - 4:
        fine = f" (currently {step} mirror{'' if step == 1 else 's'} per code"
        fine += f", {step * scale:.1f} camera px)" if scale else ")"
        return (f"the first {last + 1} planes are fine and the last "
                f"{len(cov) - last - 1} kill it — the stripes are finer than "
                f"the optics resolve{fine}. Raise `step`, or focus the relay")
    which = "x" if last < nbx else "y"
    return (f"coverage dies at plane {last + 1} of {len(cov)}, in the {which} "
            f"block — the stripes at that scale are not resolved")


def correspondences(dmd_x, dmd_y, valid, *, step: int = 8):
    """Valid pixels as (camera_xy, dmd_xy) point arrays, subsampled by `step`.

    Subsampled because a full frame is millions of points and the fit converges
    on thousands; `step` trades fit time against coverage, not accuracy.
    """
    sub = np.zeros_like(valid)
    sub[::step, ::step] = True
    sel = valid & sub
    ys, xs = np.nonzero(sel)
    cam = np.column_stack((xs, ys)).astype(np.float64)
    dmd = np.column_stack((dmd_x[sel], dmd_y[sel])).astype(np.float64)
    return cam, dmd


# ══════════════════════════════════════════════════════════════════════════════
#  Centre-out probe
# ══════════════════════════════════════════════════════════════════════════════
#
# Grow a shape outward from the centre of the panel and watch what the camera
# does. Operator's idea (2026-08-24), and it earns its place three times over:
#
#   * it is the SMALLEST first actuation that answers "do the two fields
#     overlap" — a spot at 8 % of the panel puts well under 1 % of a full
#     checkerboard's light on the sample, and on an in-vivo rig the first
#     emission is the one worth making small;
#   * ONE AXIS AT A TIME (operator, 2026-08-24), so each axis is established on
#     its own. A disc conflates them: it cannot tell an anisotropic relay from a
#     clipped one, and its equivalent-area radius hides both. A bar grown along
#     DMD-x has one honest answer per step;
#   * a rotation falls out for free. Growing a bar along DMD-x moves the camera
#     image along a fixed direction; the angle of that direction IS the
#     rotation, measured before any Gray coding rather than inferred from the
#     homography afterwards.
#
# It does NOT replace `checkerboard_pair`: a centred bar is symmetric, so it
# cannot catch a mirror flip, and one pair still gives the whole field extent in
# two exposures. This runs first because it is cheap, dim and per-axis.

# Fractions of the half-extent along the axis being grown. Weighted towards the
# SMALL end on purpose: where the DMD overfills the camera every large bar runs
# off the frame and is discarded, and the rig's first run left only two usable y
# steps out of five (2026-08-24). Small bars are also the dim ones.
PROBE_FRACS = (0.08, 0.14, 0.22, 0.32, 0.45, 0.62, 0.80, 1.00)
# The bar's fixed half-extent across that axis, as a fraction of the panel's.
# Small enough that the bar is unambiguously long, big enough to survive the
# modulation threshold on a dim relay.
PROBE_CROSS = 0.06
# The "is anything there at all" spot, before either axis is swept.
PROBE_SPOT = 0.10


def disc(width: int, height: int, radius: float, *,
         cx: float | None = None, cy: float | None = None) -> np.ndarray:
    """A filled circle of `radius` mirrors, centred on the panel by default."""
    cx = (width - 1) / 2.0 if cx is None else cx
    cy = (height - 1) / 2.0 if cy is None else cy
    y, x = np.ogrid[:height, :width]
    return np.where((x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2,
                    ON, OFF).astype(np.uint8)


def axis_bar(width: int, height: int, axis: int, half_extent: float, *,
             cross_frac: float = PROBE_CROSS) -> np.ndarray:
    """A centred bar `half_extent` mirrors long along `axis` (0 = x, 1 = y)."""
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    y, x = np.ogrid[:height, :width]
    if axis == 0:
        long_ok = np.abs(x - cx) <= half_extent
        cross_ok = np.abs(y - cy) <= cross_frac * height / 2.0
    else:
        long_ok = np.abs(y - cy) <= half_extent
        cross_ok = np.abs(x - cx) <= cross_frac * width / 2.0
    return np.where(long_ok & cross_ok, ON, OFF).astype(np.uint8)


@dataclass
class ProbeStep:
    """One projected shape, and what the camera saw of it.

    `cov` is the lit region's 2x2 second-moment matrix in camera px, kept
    whole rather than reduced here: the extent has to be measured along a
    direction that is only known once the LARGEST step has been seen, so the
    per-step reduction cannot happen while the step is being taken.
    """
    axis:        int                        # 0 = DMD x, 1 = DMD y, -1 = the spot
    frac:        float
    dmd_extent:  float                      # mirrors, half-extent along `axis`
    lit_px:      int
    lit_frac:    float                      # of the whole camera frame
    box:         tuple[int, int, int, int] | None
    cam_cx:      float                      # centroid, camera px
    cam_cy:      float
    cov:         tuple                      # ((sxx, sxy), (sxy, syy)), camera px
    clipped:     bool                       # its lit area touches the frame edge
    cam_shape:   tuple[int, int] = (0, 0)   # so a step describes its own frame


def _measure(mask: np.ndarray) -> tuple[float, float, tuple, bool]:
    """(cx, cy, covariance, clipped) of the lit region."""
    n = int(mask.sum())
    box = bounding_box(mask)
    clipped = bool(box is not None and (box[0] == 0 or box[1] == 0
                                        or box[2] == mask.shape[1]
                                        or box[3] == mask.shape[0]))
    if n < 3:
        return float("nan"), float("nan"), ((0.0, 0.0), (0.0, 0.0)), clipped
    ys, xs = np.nonzero(mask)
    cx, cy = float(xs.mean()), float(ys.mean())
    dx, dy = xs - cx, ys - cy
    c = ((float((dx * dx).mean()), float((dx * dy).mean())),
         (float((dx * dy).mean()), float((dy * dy).mean())))
    return cx, cy, c, clipped


def centre_out_probe(project: Callable[[np.ndarray], None],
                     grab: Callable[[], np.ndarray],
                     dmd_size: tuple[int, int], *,
                     fracs=PROBE_FRACS,
                     min_modulation: float = MIN_MODULATION,
                     log: Callable[[str], None] = print) -> list[ProbeStep]:
    """A dim spot, then a bar grown along x, then one along y → a row each.

    One dark reference is grabbed first and reused throughout: the sample does
    not change between exposures, so `(lit - dark) / (lit + dark)` cancels it
    exactly as a complementary pair would, at half the exposures.

    **This actuates** — the caller owns that decision (§2).
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    half = (w / 2.0, h / 2.0)

    project(_blank(w, h))
    dark = np.asarray(grab(), dtype=np.float32)

    def shot(frame, axis, frac, extent) -> ProbeStep:
        project(frame)
        lit = np.asarray(grab(), dtype=np.float32)
        if lit.shape != dark.shape:
            raise CalibrationError(
                f"the camera changed shape mid-probe ({dark.shape} -> "
                f"{lit.shape}) — its settings must hold for the whole sweep")
        m = field_mask(lit, dark, min_modulation=min_modulation)
        n = int(m.sum())
        cx, cy, cov, clipped = _measure(m)
        s = ProbeStep(axis=axis, frac=float(frac), dmd_extent=float(extent),
                      lit_px=n, lit_frac=float(n) / m.size,
                      box=bounding_box(m), cam_cx=cx, cam_cy=cy, cov=cov,
                      clipped=clipped, cam_shape=(m.shape[0], m.shape[1]))
        name = "spot" if axis < 0 else f"{'xy'[axis]}={frac:4.2f}"
        log(f"[dmd-probe] {name:>8} ({extent:6.1f} mirrors) -> {n:>9d} px lit "
            f"({100 * s.lit_frac:5.1f}% of frame)"
            + ("" if n < 3 else f", centre ({cx:.0f}, {cy:.0f})")
            + (" [touches the frame edge]" if clipped else ""))
        return s

    out = [shot(disc(w, h, PROBE_SPOT * min(half)), -1, PROBE_SPOT,
                PROBE_SPOT * min(half))]
    if out[0].lit_px == 0:
        # Say it here rather than after ten more exposures: a spot at the panel
        # centre that the camera cannot see is the whole answer.
        log("[dmd-probe] the centre spot is invisible — the axis sweeps will "
            "show whether anything reaches the camera at all")
    for axis in (0, 1):
        for frac in fracs:
            extent = frac * half[axis]
            out.append(shot(axis_bar(w, h, axis, extent), axis, frac, extent))
    return out


def _extent_along(cov: tuple, u: np.ndarray) -> float:
    """Half-extent of a uniform region along unit vector `u`, in camera px.

    A uniform rectangle of half-extent A has variance A^2/3 along that axis, so
    the second moment is what converts back. Second moments rather than the
    bounding box because a rotated bar's box grows in BOTH camera axes.
    """
    c = np.asarray(cov, dtype=np.float64)
    return float(np.sqrt(max(0.0, 3.0 * (u @ c @ u))))


def axis_direction(steps: list[ProbeStep], axis: int) -> np.ndarray | None:
    """Unit vector in camera px that DMD `axis` runs along.

    Taken from the LARGEST unclipped step, where the bar is unambiguously
    longer than it is wide; at the smallest step the two are comparable and the
    principal axis can be the cross one.
    """
    use = [s for s in steps if s.axis == axis and s.lit_px >= 3 and not s.clipped]
    if not use:
        return None
    c = np.asarray(max(use, key=lambda s: s.dmd_extent).cov, dtype=np.float64)
    vals, vecs = np.linalg.eigh(c)
    if vals[-1] <= 0:
        return None
    return vecs[:, -1]


def axis_scale(steps: list[ProbeStep], axis: int) -> float | None:
    """Camera px per mirror along DMD `axis`.

    Through the origin: the bar is centred, so its half-extent and its image's
    are proportional with no intercept, and a two-parameter fit over five points
    would just absorb the clipping into the intercept.
    """
    fit = axis_scale_fit(steps, axis)
    return fit[0] if fit else None


def axis_scale_fit(steps: list[ProbeStep],
                   axis: int) -> tuple[float, float, int] | None:
    """(px per mirror, rms residual in camera px, points used).

    The residual is the point, exactly as it is for `fit_transform`: a scale
    with no residual cannot be judged, and on a rig where the DMD overfills the
    camera only two or three steps survive unclipped — few enough that a bad one
    would pass unnoticed.
    """
    u = axis_direction(steps, axis)
    if u is None:
        return None
    use = [s for s in steps if s.axis == axis and s.lit_px >= 3 and not s.clipped]
    if len(use) < 2:
        return None
    x = np.array([s.dmd_extent for s in use])
    y = np.array([_extent_along(s.cov, u) for s in use])
    if (x @ x) <= 0:
        return None
    k = float((x @ y) / (x @ x))
    rms = float(np.sqrt(np.mean((y - k * x) ** 2)))
    return k, rms, len(use)


def axis_angle_deg(steps: list[ProbeStep], axis: int) -> float | None:
    """Clockwise angle from camera-x to DMD `axis`, in degrees.

    Camera y runs downward, so a positive angle here is clockwise on screen —
    the same convention `build_frame` and the standalone GUI use. The
    eigenvector's sign is arbitrary, so this folds to (-90, 90]: a bar is
    symmetric and cannot distinguish its two ends.
    """
    u = axis_direction(steps, axis)
    if u is None:
        return None
    ang = float(np.degrees(np.arctan2(u[1], u[0])))
    return ang - 180.0 if ang > 90 else (ang + 180.0 if ang <= -90 else ang)


def probe_verdict(steps: list[ProbeStep], cam_shape: tuple[int, int],
                  dmd_size: tuple[int, int]) -> str:
    """A line per axis: does it reach, at what scale, and is it clipped."""
    if not steps or not any(s.lit_px for s in steps):
        return ("the projector does not modulate the camera at all — check "
                "that the DMD is displaying, the illumination is on and the "
                "camera is exposing")
    ch, cw = int(cam_shape[0]), int(cam_shape[1])
    lit = [s for s in steps if s.lit_px >= 3]
    parts = []
    if lit:
        parts.append(f"the DMD centre lands at ({lit[0].cam_cx:.0f}, "
                     f"{lit[0].cam_cy:.0f}) in a {cw}x{ch} frame")
    ax, ay = axis_angle_deg(steps, 0), axis_angle_deg(steps, 1)
    if ax is not None:
        parts.append(f"DMD-x runs at {ax:+.1f}deg to camera-x")
    if ax is not None and ay is not None:
        # Both folded to (-90, 90], so the separation of two undirected lines
        # is the smaller of the gap and its supplement.
        d = abs(ay - ax)
        sep = min(d, 180.0 - d)
        if abs(sep - 90.0) > 1.0:
            parts.append(f"the DMD axes are {sep:.1f}deg apart, not 90 — there "
                         f"is keystone, and an affine fit will show it as "
                         f"residual")
    for axis, half_cam in ((0, cw / 2.0), (1, ch / 2.0)):
        name = "xy"[axis]
        k = axis_scale(steps, axis)
        last = [s for s in steps if s.axis == axis]
        if k is None:
            parts.append(f"{name}: too few unclipped steps to measure a scale")
            continue
        reach = k * dmd_size[axis] / 2.0        # half the panel, in camera px
        clipped = bool(last and last[-1].clipped)
        parts.append(f"{name}: {k:.3f} px/mirror, the panel's half-width is "
                     f"{reach:.0f} px against the frame's {half_cam:.0f} "
                     + ("(CLIPPED — the DMD reaches past the camera's view)"
                        if clipped or reach > half_cam else "(fits)"))
    kx, ky = axis_scale(steps, 0), axis_scale(steps, 1)
    if kx and ky and abs(kx - ky) > 0.05 * max(kx, ky):
        parts.append(f"the two axes differ by {100 * abs(kx - ky) / max(kx, ky):.0f}% "
                     f"— the relay is anisotropic, so an affine model is the "
                     f"floor, not a homography's luxury")
    return "; ".join(parts)


# Stripe offsets, as fractions of the half-extent along the axis being stepped.
STRIPE_OFFSETS = (-0.80, -0.60, -0.40, -0.20, 0.0, 0.20, 0.40, 0.60, 0.80)
STRIPE_WIDTH = 0.05         # stripe thickness, fraction of the panel's extent
STRIPE_CROSS = 0.30         # its length across the other axis


def offset_stripe(width: int, height: int, axis: int, offset: float, *,
                  thick_frac: float = STRIPE_WIDTH,
                  cross_frac: float = STRIPE_CROSS) -> np.ndarray:
    """A narrow stripe `offset` mirrors from the panel centre along `axis`."""
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    y, x = np.ogrid[:height, :width]
    half = thick_frac * (width if axis == 0 else height) / 2.0
    cross = cross_frac * (height if axis == 0 else width) / 2.0
    if axis == 0:
        m = (np.abs(x - cx - offset) <= half) & (np.abs(y - cy) <= cross)
    else:
        m = (np.abs(y - cy - offset) <= half) & (np.abs(x - cx) <= cross)
    return np.where(m, ON, OFF).astype(np.uint8)


def stripe_sweep(project: Callable[[np.ndarray], None],
                 grab: Callable[[], np.ndarray],
                 dmd_size: tuple[int, int], *,
                 offsets=STRIPE_OFFSETS,
                 min_modulation: float = MIN_MODULATION,
                 log: Callable[[str], None] = print) -> dict:
    """Step a narrow stripe across each axis → {axis: [(offset, cx, cy), …]}.

    **This replaced growing bars, and the reason is worth keeping.** A centred
    bar that grows should keep its centroid fixed; on the rig it drifted 527 px
    across the sweep (2026-08-24), because the frame clips one side of it while
    vignetting eats the other. Second moments of a lopsided region measure the
    lopsidedness, and the scale fit inherited all of it — rms 66 px.

    A narrow stripe has none of that. Its centroid is a *local* measurement, so
    vignetting shifts it by a fraction of its own width; a stripe that falls off
    the frame is simply dropped rather than silently biasing the fit; and
    because the offsets are **signed**, the direction comes out of the fit with
    its sign already attached — no eigenvector, and no handedness probe.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    half = (w / 2.0, h / 2.0)

    project(_blank(w, h))
    dark = np.asarray(grab(), dtype=np.float32)

    out: dict = {0: [], 1: []}
    for axis in (0, 1):
        for frac in offsets:
            d = frac * half[axis]
            project(offset_stripe(w, h, axis, d))
            lit = np.asarray(grab(), dtype=np.float32)
            m = field_mask(lit, dark, min_modulation=min_modulation)
            n = int(m.sum())
            box = bounding_box(m)
            edge = bool(box is not None
                        and (box[0] == 0 or box[1] == 0
                             or box[2] == m.shape[1] or box[3] == m.shape[0]))
            ys, xs = (np.nonzero(m) if n else (np.empty(0), np.empty(0)))
            cx = float(xs.mean()) if n else float("nan")
            cy = float(ys.mean()) if n else float("nan")
            keep = n >= 50 and not edge
            log(f"[dmd-stripe] {'xy'[axis]} {frac:+.2f} ({d:+7.1f} mirrors) -> "
                + (f"{n:>8d} px at ({cx:7.1f}, {cy:7.1f})" if n else
                   f"{'invisible':>28}")
                + ("" if keep else "   [dropped: "
                   + ("off the frame edge" if edge else "too little light")
                   + "]"))
            if keep:
                out[axis].append((d, cx, cy))
    return out


def fit_axis_line(points: list[tuple]) -> tuple | None:
    """[(offset, cx, cy)] → (origin, direction px/mirror, rms residual, n).

    A straight line through signed offsets: `direction` carries both the scale
    (its length) and which way the axis runs (its sign), which is the whole
    reason stripes beat symmetric bars.
    """
    if len(points) < 3:
        return None
    d = np.array([p[0] for p in points], float)
    P = np.array([[p[1], p[2]] for p in points], float)
    A = np.column_stack([d, np.ones(len(d))])
    sol, *_ = np.linalg.lstsq(A, P, rcond=None)
    direction, origin = sol[0], sol[1]
    resid = P - (A @ sol)
    rms = float(np.sqrt(np.mean((resid ** 2).sum(axis=1))))
    return origin, direction, rms, len(points)


def coarse_calibration(project: Callable[[np.ndarray], None],
                       grab: Callable[[], np.ndarray],
                       dmd_size: tuple[int, int], *,
                       offsets=STRIPE_OFFSETS,
                       min_modulation: float = MIN_MODULATION,
                       log: Callable[[str], None] = print) -> DmdCalibration:
    """An affine registration from STRIPES ALONE — no Gray coding.

    A narrow stripe stepped across each axis gives a direct
    (mirror offset -> camera position) point per exposure. Two straight-line
    fits then hand over everything an affine has: where the panel centre lands,
    how far the image moves per mirror on each axis, and which way each axis
    runs. Six parameters, ~19 exposures, none of them finer than the relay can
    resolve.

    **Prefer this when the Gray sweep cannot decode**, which is the normal case
    on a relay that does not resolve single mirrors — on this rig at ~4.4 camera
    px per mirror the sweep returned 0.0 % while stripes are perfectly legible.
    What it gives up is real:

      * **no keystone.** An affine has no perspective term, so if the panel is
        tilted relative to the sample this is wrong towards the edges — by an
        amount `run_calibration`'s residual would have told you and this cannot.
      * **a residual over ~18 points**, not thousands. Read it: it is in camera
        px and it is the scatter of the stripe centroids about a straight line,
        so a few px is good and tens of px means the stripes are not being
        measured cleanly.

    So it is a *coarse* calibration, named that in the file, and good enough to
    aim an ROI when the alternative is nothing at all.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])
    seen = stripe_sweep(project, grab, (w, h), offsets=offsets,
                        min_modulation=min_modulation, log=log)

    fits = {}
    for axis in (0, 1):
        f = fit_axis_line(seen[axis])
        if f is None:
            raise CalibrationError(
                f"only {len(seen[axis])} usable stripe(s) on the "
                f"{'xy'[axis]} axis — need 3. Every other stripe was off the "
                f"frame or too dim, which means the DMD field and the camera's "
                f"view barely overlap on that axis.")
        fits[axis] = f
        origin, direction, rms, n = f
        log(f"[dmd-calib] {'xy'[axis]}: {np.hypot(*direction):.3f} px/mirror "
            f"along ({direction[0]:+.3f}, {direction[1]:+.3f}), "
            f"residual {rms:.2f} px over {n} stripes")

    ox, dx_dir, rms_x, n_x = fits[0]
    oy, dy_dir, rms_y, n_y = fits[1]
    # Both lines pass through the panel centre at offset 0, so their origins are
    # two measurements of the same point. Disagreement is a real error bar.
    gap = float(np.hypot(*(ox - oy)))
    centre = (ox + oy) / 2.0
    log(f"[dmd-calib] panel centre at ({centre[0]:.1f}, {centre[1]:.1f}); the "
        f"two axes' estimates of it differ by {gap:.1f} px")

    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    A = np.eye(3)
    A[:2, 0] = dx_dir
    A[:2, 1] = dy_dir
    A[:2, 2] = centre - cx * dx_dir - cy * dy_dir
    if abs(float(np.linalg.det(A[:2, :2]))) < 1e-9:
        raise CalibrationError(
            "the two measured axes are parallel, so the registration cannot be "
            "inverted — one of them was not really measured")
    cam_to_dmd = np.linalg.inv(A)

    rms = float(np.sqrt((rms_x ** 2 + rms_y ** 2) / 2.0))
    kx, ky = float(np.hypot(*dx_dir)), float(np.hypot(*dy_dir))
    ang = float(np.degrees(np.arctan2(dx_dir[1], dx_dir[0])))
    log(f"[dmd-calib] coarse affine: {kx:.3f} x {ky:.3f} px/mirror, DMD-x at "
        f"{ang:+.1f}deg, residual {rms:.2f} px over {n_x + n_y} stripes")
    if rms > 10.0:
        log("[dmd-calib] WARNING: that residual is large. The stripe centroids "
            "are not falling on a straight line, so an affine does not "
            "describe this relay — check the log above for stripes that were "
            "kept but look out of place.")
    ch, cw = np.asarray(grab()).shape[:2]
    return DmdCalibration(
        cam_to_dmd=cam_to_dmd, dmd_size=(w, h), cam_size=(int(cw), int(ch)),
        model="affine-coarse", rms_px=rms, n_points=n_x + n_y,
        created=datetime.now().isoformat(timespec="seconds"),
        notes=f"COARSE — stripes only, no Gray coding, no keystone term. "
              f"{kx:.3f} x {ky:.3f} px/mirror, DMD-x {ang:+.1f}deg, "
              f"panel centre ({centre[0]:.0f}, {centre[1]:.0f}), the two axes' "
              f"centre estimates {gap:.1f} px apart")


# ══════════════════════════════════════════════════════════════════════════════
#  Fit
# ══════════════════════════════════════════════════════════════════════════════

def _affine(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    n = len(src)
    A = np.zeros((2 * n, 6))
    b = np.empty(2 * n)
    A[0::2, 0:2], A[0::2, 2] = src, 1.0
    A[1::2, 3:5], A[1::2, 5] = src, 1.0
    b[0::2], b[1::2] = dst[:, 0], dst[:, 1]
    p = np.linalg.lstsq(A, b, rcond=None)[0]
    return np.array([[p[0], p[1], p[2]], [p[3], p[4], p[5]], [0.0, 0.0, 1.0]])


def _normalise(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Hartley normalisation — centroid at 0, mean radius √2. Without it the
    DLT's design matrix is badly conditioned at pixel magnitudes."""
    c = p.mean(axis=0)
    d = float(np.sqrt(((p - c) ** 2).sum(axis=1)).mean())
    s = np.sqrt(2.0) / max(d, 1e-12)
    T = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1.0]])
    return (p - c) * s, T


def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    sn, Ts = _normalise(src)
    dn, Td = _normalise(dst)
    n = len(src)
    A = np.zeros((2 * n, 9))
    x, y = sn[:, 0], sn[:, 1]
    u, v = dn[:, 0], dn[:, 1]
    A[0::2] = np.column_stack([-x, -y, -np.ones(n), np.zeros((n, 3)),
                               u * x, u * y, u])
    A[1::2] = np.column_stack([np.zeros((n, 3)), -x, -y, -np.ones(n),
                               v * x, v * y, v])
    # full_matrices=False: only the 9x9 Vh is wanted, and the default would
    # build a (2n, 2n) U — 300 MB and seconds at a few thousand points.
    H = np.linalg.svd(A, full_matrices=False)[2][-1].reshape(3, 3)
    H = np.linalg.inv(Td) @ H @ Ts
    return H / H[2, 2]


def apply_transform(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Map (N, 2) points through a 3×3 homogeneous matrix."""
    p = np.asarray(pts, dtype=np.float64)
    h = np.column_stack([p, np.ones(len(p))]) @ np.asarray(M).T
    w = np.where(np.abs(h[:, 2]) < 1e-12, 1e-12, h[:, 2])
    return h[:, :2] / w[:, None]


def fit_transform(src: np.ndarray, dst: np.ndarray, *, model: str = "affine",
                  reject_sigma: float = 3.0, iters: int = 3):
    """Least-squares src → dst → (M, rms_px, inliers).

    **The residual is the point.** Four corner marks fit a homography exactly
    and report rms 0 right or wrong; a dense Gray-code fit reports a residual
    that says whether the model is adequate — an affine rms of several px on a
    well-decoded frame means the optics are not affine, not that it is noisy.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    if len(src) != len(dst):
        raise ValueError(f"{len(src)} src points vs {len(dst)} dst")
    if model not in ("affine", "homography"):
        raise ValueError(f"model must be affine/homography, got {model!r}")
    need = 3 if model == "affine" else 4
    if len(src) < need:
        raise ValueError(f"{model} needs >= {need} points, got {len(src)}")
    fit = _affine if model == "affine" else _homography

    keep = np.ones(len(src), bool)
    M = fit(src, dst)
    for _ in range(max(0, iters)):
        err = np.hypot(*(apply_transform(M, src) - dst).T)
        rms = float(np.sqrt(np.mean(err[keep] ** 2)))
        if rms <= 0:
            break
        nk = keep & (err <= reject_sigma * rms)
        if nk.sum() < need or nk.sum() == keep.sum():
            break
        keep = nk
        M = fit(src[keep], dst[keep])
    err = np.hypot(*(apply_transform(M, src) - dst).T)
    return M, float(np.sqrt(np.mean(err[keep] ** 2))), keep


# ══════════════════════════════════════════════════════════════════════════════
#  Using it
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DmdCalibration:
    """A measured DMD↔camera registration, and what it took to get it.

    The residual and point count are stored alongside the matrix: a transform
    with no provenance cannot be judged later, and "rms 0.4 px over 4100 points"
    is the difference between trusting it and re-running it.
    """
    cam_to_dmd: np.ndarray            # 3×3, camera px → DMD mirrors
    dmd_size:   tuple[int, int]       # (width, height) mirrors
    cam_size:   tuple[int, int]       # (width, height) px
    model:      str = "homography"
    rms_px:     float = 0.0
    n_points:   int = 0
    created:    str = ""
    notes:      str = ""

    def __post_init__(self) -> None:
        # Keyed by camera shape. Safe because a calibration is a measurement:
        # a new registration is a new object, never an edit to this one.
        self._mask_cache: dict[tuple[int, int], np.ndarray] = {}

    @property
    def dmd_to_cam(self) -> np.ndarray:
        return np.linalg.inv(self.cam_to_dmd)

    # ── the accessible area ──────────────────────────────────────────────────
    def accessible(self, pts: np.ndarray) -> np.ndarray:
        """Which camera points the DMD can actually illuminate.

        Answered by mapping into mirror space and bounds-checking, not by a
        polygon test — the transform already knows the shape of the field, and
        under keystone that shape is not a rectangle.
        """
        d = apply_transform(self.cam_to_dmd, np.atleast_2d(pts))
        w, h = self.dmd_size
        return ((d[:, 0] >= 0) & (d[:, 0] <= w - 1)
                & (d[:, 1] >= 0) & (d[:, 1] <= h - 1))

    _MASK_ROWS = 256            # rows per band; caps the transform's temporaries

    def accessible_mask(self, shape: tuple[int, int]) -> np.ndarray:
        """(H, W) bool: the camera pixels an ROI may legally cover.

        Read-only, cached per shape, and built in row bands. The ROI editor asks
        for this on **every drag** (`_refresh_status` → `clipped_mask`), and the
        whole-grid version cost 798 ms and ~1 GB per call at ORCA full frame —
        it would have made the editor unusable the first time it met a real
        camera. Banding keeps the temporaries at a few MB; the cache means a
        drag pays nothing at all.
        """
        h, w = int(shape[0]), int(shape[1])
        hit = self._mask_cache.get((h, w))
        if hit is not None:
            return hit

        M = np.asarray(self.cam_to_dmd, dtype=np.float64)
        dw, dh = self.dmd_size
        x = np.arange(w, dtype=np.float64)
        out = np.empty((h, w), dtype=bool)
        for y0 in range(0, h, self._MASK_ROWS):
            y = np.arange(y0, min(y0 + self._MASK_ROWS, h),
                          dtype=np.float64)[:, None]
            den = M[2, 0] * x + M[2, 1] * y + M[2, 2]
            den = np.where(np.abs(den) < 1e-12, 1e-12, den)
            dx = (M[0, 0] * x + M[0, 1] * y + M[0, 2]) / den
            dy = (M[1, 0] * x + M[1, 1] * y + M[1, 2]) / den
            out[y0:y0 + y.shape[0]] = ((dx >= 0) & (dx <= dw - 1)
                                       & (dy >= 0) & (dy <= dh - 1))
        # Shared, so nobody may edit it in place.
        out.flags.writeable = False
        self._mask_cache[(h, w)] = out
        return out

    def accessible_corners(self) -> np.ndarray:
        """The DMD field's four corners in camera px, for drawing its outline."""
        w, h = self.dmd_size
        return apply_transform(
            self.dmd_to_cam,
            np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float))

    # ── persistence ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"cam_to_dmd": np.asarray(self.cam_to_dmd).tolist(),
                "dmd_size": list(self.dmd_size), "cam_size": list(self.cam_size),
                "model": self.model, "rms_px": float(self.rms_px),
                "n_points": int(self.n_points), "created": self.created,
                "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> "DmdCalibration":
        return cls(cam_to_dmd=np.array(d["cam_to_dmd"], float),
                   dmd_size=tuple(d["dmd_size"]), cam_size=tuple(d["cam_size"]),
                   model=d.get("model", "homography"),
                   rms_px=float(d.get("rms_px", 0.0)),
                   n_points=int(d.get("n_points", 0)),
                   created=d.get("created", ""), notes=d.get("notes", ""))

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "DmdCalibration":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def describe(self) -> str:
        return (f"DMD {self.dmd_size[0]}x{self.dmd_size[1]} → camera "
                f"{self.cam_size[0]}x{self.cam_size[1]}, {self.model}, "
                f"rms {self.rms_px:.2f} px over {self.n_points} points"
                + (f" ({self.created})" if self.created else ""))


def run_calibration(project: Callable[[np.ndarray], None],
                    grab: Callable[[], np.ndarray],
                    dmd_size: tuple[int, int], *,
                    model: str = "homography",
                    square: int = 64,
                    step: int = 8,
                    probe: bool = True,
                    gray_step: int = 0,     # 0 = size it from the probe's scale
                    min_modulation: float = MIN_MODULATION,
                    log: Callable[[str], None] = print) -> DmdCalibration:
    """Project a sweep, image it, and return the registration.

    `project(frame)` displays one device-sized frame; `grab()` returns the
    camera's view of whatever is currently displayed. Taking both as callables
    keeps this testable against a simulated camera — the whole pipeline can be
    held to a transform we chose before any light is emitted — and keeps the
    hardware out of the algorithm (§5b A3's lesson, applied up front).

    Stages, dimmest first, so a misaimed rig fails on twelve small exposures
    rather than forty-two full-panel ones: the centre-out probe, then one
    complementary checkerboard pair for the field extent, then the Gray sweep.

    **The caller owns the actuation.** By the time this is called the decision
    to emit light has been made; it projects on every plane.
    """
    w, h = int(dmd_size[0]), int(dmd_size[1])

    def shot(frame: np.ndarray) -> np.ndarray:
        project(frame)
        # COPY, in the camera's own dtype. The copy is not optional: `grab()`
        # may hand back a view into a driver buffer that the next exposure
        # overwrites, and all 40 shots are held until the decode. Promoting to
        # float64 here — as this used to — made that 3.4 GB instead of 0.8 GB at
        # ORCA full frame, and `decode`/`modulation` convert per plane anyway.
        return np.array(grab())

    # 1. centre-out, one axis at a time: the dimmest exposures come first.
    verdict, scale = "", None
    if probe:
        steps = centre_out_probe(project, grab, (w, h),
                                 min_modulation=min_modulation, log=log)
        verdict = probe_verdict(steps, steps[0].cam_shape, (w, h))
        log(f"[dmd-calib] probe: {verdict}")
        if not any(s.lit_px for s in steps):
            raise CalibrationError(
                "the centre-out probe saw nothing: " + verdict)
        # The probe measured px/mirror, so the Gray code can be sized to the
        # optics instead of to the panel. This is the difference between a
        # sweep that decodes and one that returns 0.0 % (see gray_planes).
        ks = [k for k in (axis_scale(steps, 0), axis_scale(steps, 1)) if k]
        scale = min(ks) if ks else None

    # 2. one complementary pair: the whole field extent, and the corner marks
    #    that a symmetric bar cannot give — they are what catches a mirror flip.
    log("[dmd-calib] field extent (1 pair)")
    a, b = checkerboard_pair(w, h, square)
    ia, ib = shot(a), shot(b)
    cam_shape = ia.shape
    lit = field_mask(ia, ib, min_modulation=min_modulation)
    frac = float(lit.mean())
    box = bounding_box(lit)
    log(f"[dmd-calib] {100 * frac:.1f}% of the frame is modulated, bbox {box}")
    # The probe already predicted how much of the frame the field covers. When
    # the two disagree badly the checkerboard's "field" is scattered noise, not
    # a field — and it decodes to nothing. Say so here, not 40 exposures later.
    if scale is not None:
        want = min(1.0, (scale * w) * (scale * h) / float(np.prod(cam_shape)))
        if want > 0.5 and frac < 0.5 * want:
            log(f"[dmd-calib] WARNING: the probe put the field at ~{100*want:.0f}% "
                f"of the frame but only {100*frac:.1f}% modulates. Scattered "
                f"modulation over a full-frame bbox is noise, and 20 planes of "
                f"it intersect to nothing — expect the decode to fail.")
    if frac < 0.01:
        raise CalibrationError(
            "the projector does not modulate the camera at all — check that "
            "the DMD is displaying, the illumination is on, the camera is "
            "exposing, and that the two fields overlap")

    # 3. Gray sweep, coded no finer than the optics were MEASURED to resolve.
    if gray_step:
        gstep = int(gray_step)
    else:
        # From the FINEST candidate upward, not from a predicted one: the
        # measurement is cheap and it finds the best code the optics support,
        # where a rule of thumb would only ever confirm itself.
        gstep = resolve_gray_step(project, grab, (w, h), field=frac,
                                  min_modulation=min_modulation, log=log)
    planes, nbx, nby = gray_planes(w, h, step=gstep)
    log(f"[dmd-calib] {2 * len(planes)} exposures ({nbx} x-bits, {nby} y-bits, "
        f"{gstep} mirror{'' if gstep == 1 else 's'} per code"
        + (f" = {gstep * scale:.1f} camera px)" if scale else ")"))
    on, off = [], []
    for i, p in enumerate(planes):
        on.append(shot(p))
        off.append(shot((255 - p).astype(np.uint8)))
    dx, dy, valid = decode(on, off, nbx, nby, min_modulation=min_modulation)
    log(f"[dmd-calib] {100 * valid.mean():.1f}% of pixels decoded")

    # Codes → mirrors. The centre of the code's cell, not its edge.
    if gstep > 1:
        dx = dx * gstep + (gstep - 1) / 2.0
        dy = dy * gstep + (gstep - 1) / 2.0

    cam, dmd = correspondences(dx, dy, valid, step=step)
    if len(cam) < 20:
        # Report WHICH plane killed it before raising. The exposures are already
        # paid for, and "check focus and exposure" sent a real session looking
        # at the wrong thing after the probe had just succeeded.
        cov = plane_coverage(on, off, min_modulation=min_modulation)
        log("[dmd-calib] plane   this plane   still valid after it")
        for i, (t, v) in enumerate(cov):
            log(f"[dmd-calib]  {i:>3}      {100 * t:6.1f}%        {100 * v:6.1f}%")
        why = _decode_diagnosis(cov, nbx, gstep, scale)
        raise CalibrationError(
            f"only {len(cam)} usable correspondences — too few to register. "
            f"{why}. (Every plane must modulate a pixel for it to count, so "
            f"one unresolved plane costs the whole frame.)")

    M, rms, keep = fit_transform(cam, dmd, model=model)
    log(f"[dmd-calib] {model}: rms {rms:.3f} px over {int(keep.sum())} inliers")
    return DmdCalibration(
        cam_to_dmd=M, dmd_size=(w, h),
        cam_size=(int(cam_shape[1]), int(cam_shape[0])),
        model=model, rms_px=rms, n_points=int(keep.sum()),
        created=datetime.now().isoformat(timespec="seconds"),
        # The probe's verdict goes in the file: it is the only record of what
        # the two fields looked like BEFORE the fit smoothed them into a matrix.
        notes=f"field {100 * frac:.1f}% of frame, bbox {box}"
              + (f"; probe: {verdict}" if verdict else ""))


def mask_from_roi(roi_cam: np.ndarray, dmd_to_cam: np.ndarray,
                  width: int, height: int) -> np.ndarray:
    """Camera-space ROI mask → the DMD frame that illuminates it.

    Iterates over *mirrors* and asks where each lands, rather than pushing ROI
    pixels forward: a forward map leaves holes wherever the DMD is coarser than
    the camera, and a mask with holes is a stimulus with holes.
    """
    roi = np.asarray(roi_cam)
    yy, xx = np.mgrid[:height, :width]
    cam = apply_transform(dmd_to_cam,
                          np.column_stack((xx.ravel(), yy.ravel())))
    cx = np.rint(cam[:, 0]).astype(np.int64)
    cy = np.rint(cam[:, 1]).astype(np.int64)
    inside = ((cx >= 0) & (cx < roi.shape[1])
              & (cy >= 0) & (cy < roi.shape[0]))
    out = np.zeros(width * height, dtype=bool)
    out[inside] = roi[cy[inside], cx[inside]].astype(bool)
    return np.where(out.reshape(height, width), ON, OFF).astype(np.uint8)
