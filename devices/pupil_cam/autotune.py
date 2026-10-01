"""Suggest tracking parameters from a handful of frames. No Qt, no EyeLoop.

EyeLoop's config is process-global, so tuning must not touch it (a live tracker
may be running). This segments the pupil itself: the darkest compact blob.

Raising the threshold grows the blob from the pupil's darkest pixels to the
pupil, then spills into the iris and on to the whole eye opening. The pupil is
round and the spill is not, so the chosen threshold is the one where the blob
is roundest (area over its enclosing circle), averaged over the frames, within
pupil-sized limits. Blur follows the noise inside the pupil. Reflection removal
turns on if a bright spot sits beside the pupil.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np

from acqApp.devices.pupil_cam.settings import PupilSettings

_MAX_FRAMES = 24
_MIN_RADIUS = 6.0            # px; smaller is a speck, not a pupil
_MAX_RADIUS_FRAC = 0.45      # of the region's shorter side
_MIN_FILL = 0.6              # area / enclosing circle; the eye opening is ~0.6
_MIN_VALID_FRAC = 0.6        # of frames a threshold must give a pupil on
_MAX_PINS = 3
NEEDS_HELP = 0.25            # confidence under this: ask the user to seed
_LEVELS_AFTER_FIRST = 4      # region search: levels looked at from the first hit


@dataclass(frozen=True)
class AutoTune:
    threshold: int
    blur: int
    cr_remove: bool
    cr_threshold: int | None          # None: leave the current one
    region: tuple[int, int, int, int] | None   # only when one was estimated
    confidence: float                 # 0..1
    notes: str
    # Reflections in the same place on most frames: (x, y, r) full-frame px.
    pins: tuple = ()

    def apply(self, st: PupilSettings) -> PupilSettings:
        kw = dict(track_threshold=self.threshold, track_blur=self.blur,
                  cr_remove=self.cr_remove)
        if self.cr_threshold is not None:
            kw["cr_threshold"] = self.cr_threshold
        if self.region is not None:
            kw.update(limit_x0=float(self.region[0]), limit_y0=float(self.region[1]),
                      limit_x1=float(self.region[2]), limit_y1=float(self.region[3]))
        if self.pins:       # none found: keep any placed by hand
            kw["cr_pins"] = [tuple(p) for p in self.pins]
        return dataclasses.replace(st, **kw)


def _estimate_region(frames: list[np.ndarray]) -> tuple[int, int, int, int] | None:
    """A box around the most pupil-like blob, for when no eye region is set:
    dark, round, filled, clearly darker than its surround, and in the same
    place across frames. Searched at <= 400 px, so a rig frame costs little.
    The box is 3 pupil radii each way, room for the eye to move."""
    import cv2
    h, w = frames[0].shape
    sc = min(1.0, 400.0 / max(h, w))
    small = [cv2.GaussianBlur(cv2.resize(f, None, fx=sc, fy=sc,
                                         interpolation=cv2.INTER_AREA), (3, 3), 0)
             for f in frames]
    sh, sw = small[0].shape
    pix = np.concatenate([f.ravel() for f in small])
    levels = np.unique(np.linspace(np.percentile(pix, 0.5),
                                   np.percentile(pix, 60), 24).astype(int))
    r_max = 0.3 * min(sh, sw)
    votes = np.zeros((sh, sw), np.float32)
    found: list[tuple[float, float, float]] = []
    for f in small:
        best = np.zeros((sh, sw), np.float32)     # one vote per place per frame
        # The pupil is the darkest round thing: only the few levels from the
        # first that shows a candidate count, or a big dim patch (a shadow,
        # the face against a bright background) outvotes it higher up.
        first = None
        for li, t in enumerate(levels):
            if first is not None and li - first >= _LEVELS_AFTER_FIRST:
                break
            m = cv2.morphologyEx((f <= t).astype(np.uint8), cv2.MORPH_OPEN,
                                 np.ones((3, 3), np.uint8))
            n, lab, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
            if n <= 1:
                continue
            bx, by, bw, bh, area = (st[1:, i] for i in range(5))
            r = np.maximum(bw, bh) / 2.0
            fill = area / (np.pi * r * r)
            # Cut by the frame edge = a corner or the background, not a pupil.
            inner = (bx > 0) & (by > 0) & (bx + bw < sw) & (by + bh < sh)
            ok = ((r >= 4) & (r <= r_max) & (fill >= 0.65) & inner
                  & (np.minimum(bw, bh) >= 0.6 * np.maximum(bw, bh)))
            counted = False
            for k in np.flatnonzero(ok) + 1:
                x, y, cw_, ch_ = st[k, :4]
                pad = int(r[k - 1] * 0.6) + 2
                xa, ya = max(0, x - pad), max(0, y - pad)
                xb, yb = min(sw, x + cw_ + pad), min(sh, y + ch_ + pad)
                inside = lab[ya:yb, xa:xb] == k
                box = f[ya:yb, xa:xb]
                ring = ~cv2.dilate(inside.astype(np.uint8),
                                   np.ones((5, 5), np.uint8)).astype(bool)
                if inside.sum() < 10 or ring.sum() < 10:
                    continue
                contrast = float(np.median(box[ring])) - float(np.median(box[inside]))
                if contrast < 3:
                    continue
                # Round and large count most; contrast only has to be clear,
                # since a dim pupil can sit a few grey levels under its iris.
                score = (float(fill[k - 1]) ** 2 * float(np.sqrt(area[k - 1]))
                         * min(contrast, 8.0) / 8.0)
                cx, cy = int(cen[k][0]), int(cen[k][1])
                if score > best[cy, cx]:
                    best[cy, cx] = score
                found.append((cen[k][0], cen[k][1], float(r[k - 1])))
                counted = True
            if counted and first is None:
                first = li
        votes += cv2.dilate(best, np.ones((5, 5), np.uint8))
    if not found or votes.max() <= 0:
        return None
    votes = cv2.GaussianBlur(votes, (0, 0), 3)
    py, px = np.unravel_index(int(np.argmax(votes)), votes.shape)
    near = [r for x, y, r in found if (x - px) ** 2 + (y - py) ** 2 <= (2 * r) ** 2]
    r = float(np.median(near)) if near else r_max / 2
    cx, cy, half = px / sc, py / sc, 3.0 * r / sc
    return (int(max(0, cx - half)), int(max(0, cy - half)),
            int(min(w, cx + half)), int(min(h, cy + half)))


def _blob(mask: np.ndarray, seed: tuple[int, int]):
    """The component at `seed`, opened (specks off) with its holes, i.e.
    reflections, filled -> (area, fill, filled mask) or None. `fill` is the
    area over its enclosing circle's: 1 for a disc."""
    import cv2
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    lab = cv2.connectedComponents(mask, connectivity=8)[1]
    k = lab[seed[1], seed[0]]
    if k == 0:
        return None
    cnts, _ = cv2.findContours((lab == k).astype(np.uint8), cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    area = float(cv2.contourArea(c))
    r = float(cv2.minEnclosingCircle(c)[1])
    if area <= 0 or r <= 0:
        return None
    filled = np.zeros(mask.shape, np.uint8)
    cv2.drawContours(filled, [c], -1, 1, -1)
    return area, area / (np.pi * r * r), filled


Seed = tuple  # (x, y, r or None) in full-frame px: the user's click/circle


def autotune(frames: list[np.ndarray],
             region: tuple[float, float, float, float] | None = None,
             seeds: list[Seed] | None = None) -> AutoTune | None:
    """`frames`: full 2-D uint8 frames (a few, spread over the clip or the
    last seconds of the live feed). `region`: the eye box if one is set, else
    it is estimated. None when no pupil-like blob could be found.

    `seeds`: when Auto alone fails, the user's help, one per frame (same
    order as `frames`): the pupil centre clicked, plus its radius if drawn.
    Centres replace the darkest-patch guess and place the region; radii pick
    the threshold whose blob matches the drawn size."""
    import cv2
    if seeds is not None:
        pairs = [(f, sd) for f, sd in zip(frames, seeds)
                 if f is not None and f.ndim == 2 and sd is not None]
        frames = [f for f, _ in pairs]
        seeds = [sd for _, sd in pairs]
    frames = [f for f in frames if f is not None and f.ndim == 2]
    if not frames:
        return None
    if seeds is None:
        step = max(1, len(frames) // _MAX_FRAMES)
        frames = frames[::step]
    frames = [np.ascontiguousarray(f, dtype=np.uint8) for f in frames]

    estimated = region is None or region[2] <= region[0] or region[3] <= region[1]
    if estimated and seeds:
        h, w = frames[0].shape
        rs = [sd[2] for sd in seeds if len(sd) > 2 and sd[2]]
        half = 3.0 * (float(np.median(rs)) if rs else 0.08 * min(h, w))
        xs, ys = [sd[0] for sd in seeds], [sd[1] for sd in seeds]
        box = (int(max(0, min(xs) - half)), int(max(0, min(ys) - half)),
               int(min(w, max(xs) + half)), int(min(h, max(ys) + half)))
    elif estimated:
        box = _estimate_region(frames)
        if box is None:
            return None
    else:
        h, w = frames[0].shape
        box = (int(max(0, region[0])), int(max(0, region[1])),
               int(min(w, region[2])), int(min(h, region[3])))
    x0, y0, x1, y1 = box
    raw = [f[y0:y1, x0:x1] for f in frames]
    if min(raw[0].shape) < 20:
        return None
    # Analysed at <= 320 px: thresholds don't depend on scale, and a large
    # region would otherwise take seconds.
    sc = min(1.0, 320.0 / max(raw[0].shape))
    if sc < 1.0:
        # Max-pooled first, so a reflection survives the shrink.
        k = int(np.ceil(1.0 / sc))
        glint_src = [cv2.resize(cv2.dilate(c, np.ones((k, k), np.uint8)), None,
                                fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
                     for c in raw]
        raw = [cv2.resize(c, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
               for c in raw]
    else:
        glint_src = raw
    crops = [cv2.GaussianBlur(c, (5, 5), 0) for c in raw]
    ch, cw = crops[0].shape
    max_area = np.pi * (min(ch, cw) * _MAX_RADIUS_FRAC) ** 2
    min_area = np.pi * (_MIN_RADIUS * sc) ** 2

    # Where to grow the blob from: the user's click, else the darkest
    # smooth patch, per frame.
    targets: list[float | None] = []
    if seeds:
        user = seeds
        seeds = []
        for sd in user:
            sx = int(np.clip((sd[0] - x0) * sc, 0, cw - 1))
            sy = int(np.clip((sd[1] - y0) * sc, 0, ch - 1))
            seeds.append((sx, sy))
            r = sd[2] if len(sd) > 2 else None
            targets.append(np.pi * (r * sc) ** 2 if r else None)
    else:
        k = max(3, int(min(ch, cw) * 0.15) | 1)
        seeds = []
        for c in crops:
            sm = cv2.GaussianBlur(c, (k, k), 0)
            sy, sx = np.unravel_index(int(np.argmin(sm)), sm.shape)
            seeds.append((int(sx), int(sy)))
            targets.append(None)

    pix = np.concatenate([c.ravel() for c in crops])
    lo = int(max(1, np.percentile(pix, 0.5)))
    hi = int(min(254, max(lo + 6, np.percentile(pix, 90))))
    ts = np.arange(lo, hi + 1)
    fill = np.zeros((len(crops), len(ts)))
    size = np.zeros((len(crops), len(ts)))  # match to a drawn size, 0..1
    for i, c in enumerate(crops):
        for j, t in enumerate(ts):
            b = _blob((c <= t).astype(np.uint8), seeds[i])
            if b is not None and min_area <= b[0] <= max_area:
                fill[i, j] = b[1]
                if targets[i]:
                    size[i, j] = max(0.0, 1.0 - abs(np.log(b[0] / targets[i])))
    drawn = [i for i, t in enumerate(targets) if t]
    if drawn:
        # The user drew the pupil: the level whose blob matches it, among
        # roughly round ones.
        score = np.where(fill[drawn] >= 0.5, size[drawn], 0.0).mean(axis=0)
        j = int(np.argmax(score))
        if score[j] <= 0.0:
            return None
        valid = np.mean(fill >= 0.5, axis=0)
    else:
        valid = np.mean(fill >= _MIN_FILL, axis=0)
        score = np.where(valid >= _MIN_VALID_FRAC, fill.mean(axis=0), 0.0)
        j = int(np.argmax(score))
        if score[j] < _MIN_FILL:
            return None
    threshold = int(ts[j])
    confidence = (float(score[j]) if drawn else
                  float(np.clip((score[j] - _MIN_FILL) / 0.3, 0.0, 1.0) * valid[j]))

    # At that level: the edge's contrast against the pupil's noise (-> blur),
    # and any bright spot beside the pupil (-> reflection removal).
    sig, steps, rings, glints, nears = [], [], [], [], []
    for i, c in enumerate(raw):
        b = _blob((crops[i] <= threshold).astype(np.uint8), seeds[i])
        if b is None:
            continue
        area, _fill, filled = b
        inside = cv2.erode(filled, np.ones((5, 5), np.uint8)).astype(bool)
        ring = (cv2.dilate(filled, np.ones((9, 9), np.uint8)).astype(bool)
                & ~cv2.dilate(filled, np.ones((3, 3), np.uint8)).astype(bool))
        if inside.sum() > 30 and ring.sum() > 30:
            cf = c.astype(np.float32)
            sig.append(float(np.std(cf[inside]
                                    - cv2.GaussianBlur(cf, (3, 3), 0)[inside])))
            rings.append(float(np.median(c[ring])))
            steps.append(rings[-1] - float(np.median(c[inside])))
        near = cv2.dilate(filled, np.ones((int(np.sqrt(area / np.pi) * 0.8) | 1,) * 2,
                                          np.uint8)).astype(bool)
        glints.append(float(glint_src[i][near].max()))
        nears.append((i, near))
    # Averaging k*k pixels into one shrinks the noise by k = 1/sc.
    noise = max(0.5, float(np.median(sig)) / sc) if sig else 2.0
    snr = (float(np.median(steps)) if steps else 0.0) / noise
    # A faint edge needs more smoothing to stay in one piece.
    blur = 1 if snr > 20 else 3 if snr > 4 else 5

    iris = float(np.median(rings)) if rings else float(np.median(pix))
    peak = float(np.median(glints)) if glints else iris
    has_glint = peak > max(threshold + 60, iris + 60)
    cr_threshold = int(np.clip(0.5 * (peak + iris), 60, 240)) if has_glint else None

    pins = (_fixed_reflections(glint_src, nears, cr_threshold, sc, (x0, y0))
            if has_glint else ())
    notes = (f"threshold {threshold}, blur {blur}, "
             f"reflections {'on' if has_glint else 'off'}"
             + (f", {len(pins)} pinned" if pins else ""))
    return AutoTune(threshold, blur, has_glint, cr_threshold,
                    box if estimated else None, confidence, notes, pins)


def _fixed_reflections(srcs, nears, threshold: int, sc: float,
                       origin: tuple[int, int]) -> tuple:
    """Bright spots beside the pupil that sit in the same place on most
    frames: the light's own reflection, which a pin removes reliably. One
    that moves with the eye is left to the automatic pass."""
    import cv2
    if len(nears) < 2:
        return ()
    spots: list[tuple[float, float, float, int]] = []   # x, y, r, frame
    for i, near in nears:
        bright = ((srcs[i] > threshold) & near).astype(np.uint8)
        n, _lab, st, cen = cv2.connectedComponentsWithStats(bright, connectivity=8)
        for k in range(1, n):
            r = float(np.sqrt(st[k, 4] / np.pi))
            spots.append((float(cen[k][0]), float(cen[k][1]), r, i))
    tol = max(2.0, 4.0 * sc)            # 4 full-frame px, at least 2 here
    clusters: list[list] = []
    for sp in spots:
        for cl in clusters:
            if np.hypot(sp[0] - cl[0][0], sp[1] - cl[0][1]) <= tol + cl[0][2]:
                cl.append(sp)
                break
        else:
            clusters.append([sp])
    out = []
    # Most persistent first; a few at most, since a pin is removed without
    # the guards that keep the automatic pass off the pupil's rim.
    clusters.sort(key=lambda cl: -len({sp[3] for sp in cl}))
    for cl in clusters[:_MAX_PINS]:
        frames = {sp[3] for sp in cl}
        if len(frames) < max(2, 0.6 * len(nears)):
            continue
        x = float(np.median([sp[0] for sp in cl])) / sc + origin[0]
        y = float(np.median([sp[1] for sp in cl])) / sc + origin[1]
        r = float(np.max([sp[2] for sp in cl])) / sc + 2.0     # halo margin
        out.append((round(x, 1), round(y, 1), round(r, 1)))
    return tuple(out)
