"""The only file in acqApp that touches EyeLoop. No Qt.

**EyeLoop is GPL-3.0 and none of it is vendored here.** This module imports it
from a clone beside the repo, which is why the licence boundary is exactly one
file wide: nothing in acqApp is a derived work, and vendoring later — if that
is ever decided — is a change to this file alone. Credit belongs to Arvin et
al. regardless of licensing; cite doi:10.1101/2020.07.03.186387.

Set `ACQAPP_EYELOOP_DIR` to point somewhere else. Without a clone the tracker
raises `EyeLoopUnavailable` and the rest of the pupil camera is unaffected.

Three things it fixes about driving `Shape` directly, all documented in
`acqApp/docs/EYELOOP.md`:

- **`params` is never reset on failure.** `Shape.fit()` catches everything and
  leaves the previous frame's answer in place, so a dead frame silently returns
  a stale fit. `track()` nulls it first, so None means None.
- **`center_adj_` blocks forever.** On any fit failure it runs `HoughCircles`
  and, per circle found, opens a modal window and calls `waitKey(0)`. Bound to
  a no-op here — the CR branch of `Shape` already does exactly that.
- **`eyeloop.config` is a process-wide global.** One tracker per process; a
  second instance would silently share `config.engine`. Guarded, not hidden.
"""
from __future__ import annotations

import os
import sys
import types
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# …/python/eyeloop — a sibling of the repo, not inside it. Patches to apply to
# a fresh clone are in acqApp/docs/eyeloop-3.14-patches.diff.
EYELOOP_DIR = Path(
    os.environ.get("ACQAPP_EYELOOP_DIR")
    or Path(__file__).resolve().parents[3] / "eyeloop")


@dataclass(frozen=True)
class PupilFit:
    """One frame's answer. Widens acqApp's `PupilResult` with the ellipse.

    `PupilResult` carries centre + a single radius; EyeLoop gives the full
    ellipse for free and pupillometry wants it, so both are kept and `radius`
    stays the mean of the semi-axes — the mapping the old contract used.
    """

    center_x: float
    center_y: float
    semi_major: float
    semi_minor: float
    angle_deg: float

    @property
    def radius(self) -> float:
        return (self.semi_major + self.semi_minor) / 2.0

    @property
    def axis_ratio(self) -> float:
        """1.0 is a circle. Far from it usually means the fit ran into eyelid."""
        hi = max(self.semi_major, self.semi_minor)
        return min(self.semi_major, self.semi_minor) / hi if hi else 0.0


class EyeLoopUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Pin:
    """A reflection the operator marked. Crop coordinates, like everything here.

    Pinned because it is *stationary*: with the head fixed, the big reflections
    off the optics do not move, and a fixed thing does not need the guards the
    automatic pass uses to protect itself from unknown bright objects.
    """

    cx: float
    cy: float
    r: float


@dataclass(frozen=True)
class GlintRemoval:
    """Corneal-reflection removal, done here because EyeLoop's own is dead code.

    Upstream has the machinery — `Shape.artefact_` paints a filled circle over
    the CR — but it is disabled in three places at once: `fit()`'s call is
    commented out, `Shape.__init__` binds `artefact` to a no-op for *both*
    types, and `artefact_` writes into `config.engine.pup_source`, which
    `engine.py` never creates. It has never run.

    What it has to beat, measured on frame 0 of both clips: the pupil interior
    sits at median 22–23 with p95 ≈ 42, and the glints are saturated at 235 —
    about 1.3 % of the pupil's pixels. Any threshold in 100–180 selects the
    same pixels, so this is not a delicate number.
    """

    enabled: bool = True
    threshold: int = 120
    pad: int = 4            # the diffraction spikes are wider than the core
    max_area: int = 600     # bigger than this is eyelid or fur, not a glint
    ring: int = 6           # width of the annulus each blob is filled from
    search_scale: float = 0.95   # how far out to look, as a fraction of radius
    pins: tuple[Pin, ...] = ()   # operator-marked; exempt from both guards


_ARMED: list[str] = []          # process-wide, because eyeloop.config is


class EyeLoopTracker:
    """Stateful — it walks out from the previous frame's centre.

    Not a pure `detect(frame)`: the old stub's signature cannot express this.
    Whoever owns one must hold it across frames and `reset()` when the seed,
    the frame size or the operator's patience changes.
    """

    def __init__(self, threshold: int = 45, blur: int = 3,
                 model: str = "ellipsoid",
                 walk_radius: tuple[int, int] = (2, 100),
                 accept_radius: tuple[float, float] = (5.0, 200.0),
                 glint: GlintRemoval | None = None) -> None:
        self.threshold = threshold
        self.blur = blur
        self.model = model
        # Two different things, and they were one name until it cost an hour:
        # `walk_radius` bounds Shape's ray walk and is clipped INTO an int
        # array (`clip_`), so floats there make every frame throw inside the
        # bare except — i.e. silently zero fits. `accept_radius` is ours, and
        # only rejects an implausible answer after the fact.
        self.walk_radius = (int(walk_radius[0]), int(walk_radius[1]))
        self.accept_radius = accept_radius
        self.glint = glint if glint is not None else GlintRemoval()
        self._shape = None
        self._size: tuple[int, int] | None = None
        self._last_radius: float | None = None
        self._last_shape: tuple[float, float, float] | None = None
        self.last_glint_mask: np.ndarray | None = None
        self.last_glint_px = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def arm(self, width: int, height: int, seed: tuple[float, float]) -> None:
        """Build the processor for a frame size. Call again if the size changes.

        `config.engine.width/height` are read when `reset()` builds the walkout
        corners, so they must be set before it — not after.
        """
        if str(EYELOOP_DIR) not in sys.path:
            if not (EYELOOP_DIR / "eyeloop").is_dir():
                raise EyeLoopUnavailable(
                    f"no EyeLoop clone at {EYELOOP_DIR}. Set one up with "
                    f"'git clone https://github.com/simonarvin/eyeloop.git' "
                    f"then 'git apply <repo>/acqApp/docs/"
                    f"eyeloop-3.14-patches.diff' inside it, or point "
                    f"ACQAPP_EYELOOP_DIR at an existing clone.")
            sys.path.insert(0, str(EYELOOP_DIR))

        import eyeloop.config as config

        # Stub what Shape reads off the globals. Must precede the import of
        # processor: Shape.__init__ reads config.arguments.model.
        config.arguments = types.SimpleNamespace(model=self.model)
        config.engine = types.SimpleNamespace(
            dataout={}, width=int(width), height=int(height), angle=0)

        import eyeloop.engine.processor as processor

        self._config = config
        self._shape = processor.Shape(type=1)
        self._shape.binarythreshold = int(self.threshold)
        self._shape.blur = (self.blur, self.blur)
        self._shape.min_radius, self._shape.max_radius = self.walk_radius

        # The landmine. See module docstring.
        self._shape.center_adj = lambda: None

        self._size = (int(width), int(height))
        self._shape.reset((float(seed[0]), float(seed[1])))

        mine = str(id(self))
        if _ARMED and _ARMED[0] != mine:
            warnings.warn(
                "a second EyeLoopTracker was armed in this process; they share "
                "eyeloop.config and will corrupt each other's frame geometry",
                RuntimeWarning, stacklevel=2)
        _ARMED[:] = [mine]

    def reset(self, seed: tuple[float, float]) -> None:
        """Re-seed without rebuilding. Cheap; call it whenever the fit is lost."""
        if self._shape is None:
            raise RuntimeError("arm() first")
        self._last_radius = None       # the old shape describes the old place
        self._last_shape = None
        self._shape.reset((float(seed[0]), float(seed[1])))

    @property
    def armed(self) -> bool:
        return self._shape is not None

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size

    # ── per frame ────────────────────────────────────────────────────────────

    def apply_settings(self, threshold: int | None = None,
                       blur: int | None = None) -> None:
        """Live knobs. Changing these does not invalidate the walk."""
        if threshold is not None:
            self.threshold = int(threshold)
            if self._shape is not None:
                self._shape.binarythreshold = int(threshold)
        if blur is not None:
            self.blur = int(blur) | 1          # cv2 kernels must be odd
            if self._shape is not None:
                self._shape.blur = (self.blur, self.blur)

    def track(self, gray: np.ndarray) -> PupilFit | None:
        """One grayscale uint8 frame in, a fit or None out.

        None is genuine: `params` is nulled first, so a stale answer cannot be
        mistaken for a fresh one.
        """
        if self._shape is None:
            raise RuntimeError("arm() first")
        if gray.ndim != 2:
            raise ValueError(f"expected a 2-D grayscale frame, got {gray.shape}")
        if gray.dtype != np.uint8:
            gray = gray.astype(np.uint8)

        h, w = gray.shape
        if self._size != (w, h):
            raise ValueError(f"armed for {self._size}, given {(w, h)}; re-arm")

        gray = self._deglint(gray)

        self._shape.fit_model.params = None     # so None means None
        self._config.engine.dataout = {}
        try:
            self._shape.track(gray)
        except Exception:
            return None

        fit = self._to_fit(self._shape.fit_model.params)
        if fit is not None:
            self._last_radius = fit.radius
            self._last_shape = (fit.semi_major, fit.semi_minor, fit.angle_deg)
        return fit

    def _deglint(self, gray: np.ndarray) -> np.ndarray:
        """Blank the corneal reflections before the walk sees them.

        Uses the *previous* frame's centre and radius — the walk is already
        built on that assumption, and a glint does not move far in 1/15 s. The
        first frame falls back to the seed and the walk's own max radius.
        """
        self.last_glint_mask, self.last_glint_px = None, 0
        if not self.glint.enabled or self._shape is None:
            return gray

        centre = self._shape.center
        try:
            cx, cy = float(centre[0]), float(centre[1])
        except (TypeError, IndexError):
            return gray            # -1 until the first reset()

        radius = self._last_radius or float(self.walk_radius[1])
        cleaned, mask = remove_glints(gray, (cx, cy), radius, self.glint,
                                      self._last_shape)
        self.last_glint_mask = mask
        self.last_glint_px = int(mask.sum())
        return cleaned

    def _to_fit(self, params) -> PupilFit | None:
        if params is None:
            return None
        try:
            (cx, cy), sw, sh, angle = params
        except (TypeError, ValueError):
            return None
        vals = (float(cx), float(cy), float(sw), float(sh), float(angle))
        if not all(np.isfinite(vals)):
            return None
        r = (vals[2] + vals[3]) / 2.0
        lo, hi = self.accept_radius
        if not (lo <= r <= hi):
            return None     # a fit far outside the plausible band is not one
        return PupilFit(*vals)


def _blank(gray, cleaned, mask, box, blob, cfg, kernel):
    """Dilate one blob, fill it from its own ring, record it. Box-local.

    Every blob gets its own small box, which is both why this is cheap and why
    the fill is honest: the ring has to be the pixels around *this* reflection,
    not the average of the whole eye.
    """
    import cv2
    y0, y1, x0, x1 = box
    if cfg.pad > 0:
        blob = cv2.dilate(blob.astype(np.uint8), kernel).astype(bool)
    sub = gray[y0:y1, x0:x1]
    free = ~blob & (sub < cfg.threshold)
    if not free.any():
        return False
    cleaned[y0:y1, x0:x1][blob] = np.uint8(np.median(sub[free]))
    mask[y0:y1, x0:x1] |= blob
    return True


def remove_glints(gray: np.ndarray, center: tuple[float, float], radius: float,
                  cfg: GlintRemoval,
                  shape: tuple[float, float, float] | None = None
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Blank the corneal reflections. Returns (cleaned, mask); input untouched.

    Two passes, and they have different rules on purpose:

    **Automatic** — bright blobs inside the fitted ellipse scaled by
    `search_scale`, small enough to be a reflection. Both guards exist because
    the blob is *unknown*: `search_scale` keeps the mask off the eyelash line
    (the State clip's is covered in specks at 0.80-0.91 r, and masking those
    erases the pupil boundary and inflates the radius), and `max_area` keeps it
    off the frame-spanning background component.

    **Pinned** — reflections the operator has marked. A pin says "this is a
    reflection, it is here, and it stays here", so **neither guard applies**:
    no reach limit, no area limit. That is the point of pinning. The big
    stationary reflections are exactly the ones the automatic pass has to be
    too timid to touch, because from the inside they look like the eyelash
    line that must not be touched.

    Everything is in the coordinates of the frame passed in, i.e. the crop.
    """
    import cv2

    h, w = gray.shape
    mask = np.zeros((h, w), bool)
    cleaned = gray.copy()
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * cfg.pad + 1, 2 * cfg.pad + 1))
    margin = cfg.pad + cfg.ring
    hit = False

    def box_around(cx0, cy0, cx1, cy1):
        return (max(0, int(cy0) - margin), min(h, int(cy1) + margin + 1),
                max(0, int(cx0) - margin), min(w, int(cx1) + margin + 1))

    # ── pinned: no reach, no area limit ──────────────────────────────────────
    for pin in cfg.pins:
        y0, y1, x0, x1 = box_around(pin.cx - pin.r, pin.cy - pin.r,
                                    pin.cx + pin.r, pin.cy + pin.r)
        if y1 - y0 < 2 or x1 - x0 < 2:
            continue
        sub = gray[y0:y1, x0:x1]
        yy, xx = np.ogrid[y0:y1, x0:x1]
        blob = (sub >= cfg.threshold) & (
            (xx - pin.cx) ** 2 + (yy - pin.cy) ** 2 <= pin.r ** 2)
        if blob.any():
            hit |= _blank(gray, cleaned, mask, (y0, y1, x0, x1), blob, cfg, kernel)

    # ── automatic: inside the pupil, small enough to be a reflection ─────────
    cx, cy = float(center[0]), float(center[1])
    a, b, phi = shape if shape else (radius, radius, 0.0)
    a = max(4.0, abs(a) * cfg.search_scale)
    b = max(4.0, abs(b) * cfg.search_scale)
    reach = max(a, b)
    ey0, ey1 = max(0, int(cy - reach)), min(h, int(cy + reach) + 1)
    ex0, ex1 = max(0, int(cx - reach)), min(w, int(cx + reach) + 1)

    if ey1 - ey0 >= 3 and ex1 - ex0 >= 3:
        roi = gray[ey0:ey1, ex0:ex1]
        yy, xx = np.ogrid[ey0:ey1, ex0:ex1]
        t = np.deg2rad(phi)
        dx, dy = xx - cx, yy - cy
        u = (dx * np.cos(t) + dy * np.sin(t)) / a
        v = (-dx * np.sin(t) + dy * np.cos(t)) / b
        inside = u * u + v * v <= 1.0

        n, labels, stats, _ = cv2.connectedComponentsWithStats(
            ((roi >= cfg.threshold) & inside).astype(np.uint8), 8)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] > cfg.max_area:
                continue
            bx, by = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
            bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
            y0, y1, x0, x1 = box_around(ex0 + bx, ey0 + by,
                                        ex0 + bx + bw, ey0 + by + bh)
            blob = np.zeros((y1 - y0, x1 - x0), bool)
            ly0, lx0 = y0 - ey0, x0 - ex0
            ly1, lx1 = min(labels.shape[0], y1 - ey0), min(labels.shape[1], x1 - ex0)
            if ly1 <= max(0, ly0) or lx1 <= max(0, lx0):
                continue
            piece = labels[max(0, ly0):ly1, max(0, lx0):lx1] == i
            oy, ox = max(0, -ly0), max(0, -lx0)
            blob[oy:oy + piece.shape[0], ox:ox + piece.shape[1]] = piece
            hit |= _blank(gray, cleaned, mask, (y0, y1, x0, x1), blob, cfg, kernel)

    return (cleaned, mask) if hit else (gray, mask)


def measure_reflection(gray: np.ndarray, at: tuple[float, float],
                       threshold: int = 120, max_r: float = 80.0,
                       pad: int = 3) -> float:
    """Radius of the bright blob the operator clicked, for sizing a pin.

    Takes the connected bright component under the click — or the nearest one
    within a few px, since nobody clicks the centre of a star exactly. Falls
    back to a small default when the click lands on nothing bright, so a pin
    always has *some* extent and the operator can see it and move it.
    """
    import cv2

    h, w = gray.shape
    x, y = int(round(at[0])), int(round(at[1]))
    if not (0 <= x < w and 0 <= y < h):
        return 8.0

    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        (gray >= threshold).astype(np.uint8), 8)
    lab = int(labels[y, x])
    if lab == 0:                     # clicked just off it — look nearby
        r = 6
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        x0, x1 = max(0, x - r), min(w, x + r + 1)
        near = labels[y0:y1, x0:x1]
        hits = near[near > 0]
        if hits.size:
            lab = int(np.bincount(hits).argmax())
    if lab == 0:
        return 8.0

    bw = stats[lab, cv2.CC_STAT_WIDTH]
    bh = stats[lab, cv2.CC_STAT_HEIGHT]
    return float(min(max_r, max(6.0, 0.5 * max(bw, bh) + pad)))


def seed_from_darkest(gray: np.ndarray, sigma: float = 6.0) -> tuple[int, int]:
    """Darkest point of a blurred frame — only trustworthy inside an eye crop.

    On a full wide-FOV rig frame this returns the *corner*: vignetting and dark
    background beat the pupil at large scale. Measured on both clips; see
    acqApp/docs/EYELOOP.md.
    """
    from scipy.ndimage import gaussian_filter
    b = gaussian_filter(gray.astype(np.float32), sigma)
    y, x = np.unravel_index(int(np.argmin(b)), b.shape)
    return int(x), int(y)
