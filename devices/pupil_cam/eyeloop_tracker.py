"""The only file in acqApp that touches EyeLoop. No Qt.

EyeLoop is GPL-3.0 and imported from a clone beside the repo, never vendored,
so the licence boundary is this one file. Cite Arvin et al.,
doi:10.1101/2020.07.03.186387. `ACQAPP_EYELOOP_DIR` overrides the location.

Three traps in driving `Shape` directly (docs/EYELOOP.md):
- `fit()` swallows failures and keeps the previous `params`, so a dead frame
  returns a stale fit. `track()` nulls it first.
- `center_adj_` opens a modal window and `waitKey(0)` on any failure. Bound to
  a no-op here.
- `eyeloop.config` is process-global: one tracker per process.
"""
from __future__ import annotations

import os
import sys
import types
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# EyeLoop's ellipse fit (models/ellipsoid.py) casts its eigen-solution to
# float on purpose and takes np.real a line later; numpy warned on every
# frame, flooding the console. Only EyeLoop's own modules are silenced.
warnings.filterwarnings("ignore", category=np.exceptions.ComplexWarning,
                        module=r"eyeloop\.")

# Patches for a fresh clone: docs/eyeloop-3.14-patches.diff.
EYELOOP_DIR = Path(
    os.environ.get("ACQAPP_EYELOOP_DIR")
    or Path(__file__).resolve().parents[3] / "eyeloop")


@dataclass(frozen=True)
class PupilFit:
    """The full ellipse; `radius` is the mean semi-axis."""

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
        """1.0 is a circle; far from it usually means the eyelid."""
        hi = max(self.semi_major, self.semi_minor)
        return min(self.semi_major, self.semi_minor) / hi if hi else 0.0


class EyeLoopUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Pin:
    """An operator-marked stationary reflection, in crop coordinates."""

    cx: float
    cy: float
    r: float


@dataclass(frozen=True)
class GlintRemoval:
    """Corneal-reflection removal. EyeLoop's own is disabled in three places
    upstream and has never run. On the test clips the pupil sits at ~22 and
    glints at 235, so any threshold in 100-180 picks the same pixels."""

    enabled: bool = True
    threshold: int = 120
    pad: int = 4            # diffraction spikes are wider than the core
    max_area: int = 600     # bigger is eyelid or fur
    ring: int = 6           # annulus each blob is filled from
    search_scale: float = 0.95   # fraction of the radius searched
    pins: tuple[Pin, ...] = ()   # exempt from both guards


_ARMED: list[str] = []          # process-wide, as eyeloop.config is


class EyeLoopTracker:
    """Stateful: walks out from the previous frame's centre. Hold one across
    frames; `reset()` when the seed or frame size changes."""

    def __init__(self, threshold: int = 45, blur: int = 3,
                 model: str = "ellipsoid",
                 walk_radius: tuple[int, int] = (2, 100),
                 accept_radius: tuple[float, float] = (5.0, 200.0),
                 glint: GlintRemoval | None = None) -> None:
        self.threshold = threshold
        self.blur = int(blur) | 1      # cv2 kernels must be odd
        self.model = model
        # `walk_radius` is clipped INTO an int array by Shape; floats make
        # every frame throw inside its bare except (silently zero fits).
        # `accept_radius` is ours: rejects an implausible answer afterwards.
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
        """Build for a frame size; call again if it changes. The fit model is
        baked into the Shape built here, so a model switch needs a re-arm."""
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

        # Before importing processor: Shape.__init__ and reset() read these.
        config.arguments = types.SimpleNamespace(model=self.model)
        config.engine = types.SimpleNamespace(
            dataout={}, width=int(width), height=int(height), angle=0)

        import eyeloop.engine.processor as processor

        self._config = config
        self._shape = processor.Shape(type=1)
        self._shape.binarythreshold = int(self.threshold)
        self._shape.blur = (self.blur, self.blur)
        self._shape.min_radius, self._shape.max_radius = self.walk_radius
        self._shape.center_adj = lambda: None      # the modal-window trap

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
        """Re-seed without rebuilding; cheap."""
        if self._shape is None:
            raise RuntimeError("arm() first")
        self._last_radius = None
        self._last_shape = None
        self._shape.reset((float(seed[0]), float(seed[1])))

    def seed(self, fit: PupilFit) -> None:
        """Carry on as if `fit` (crop px) had just been tracked: the walk
        starts at its centre and reflections are searched around it."""
        self.reset((fit.center_x, fit.center_y))
        self._last_radius = fit.radius
        self._last_shape = (fit.semi_major, fit.semi_minor, fit.angle_deg)

    @property
    def armed(self) -> bool:
        return self._shape is not None

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size

    # ── per frame ────────────────────────────────────────────────────────────

    def apply_settings(self, threshold: int | None = None,
                       blur: int | None = None) -> None:
        """Live; doesn't invalidate the walk."""
        if threshold is not None:
            self.threshold = int(threshold)
            if self._shape is not None:
                self._shape.binarythreshold = int(threshold)
        if blur is not None:
            self.blur = int(blur) | 1
            if self._shape is not None:
                self._shape.blur = (self.blur, self.blur)

    def track(self, gray: np.ndarray) -> PupilFit | None:
        """One uint8 grayscale frame -> a fit, or a genuine None."""
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
        """Blank reflections around the PREVIOUS fit (a glint barely moves
        between frames); the first frame uses the seed and max radius."""
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
            return None
        return PupilFit(*vals)


def _blank(gray, cleaned, mask, box, blob, cfg, kernel):
    """Dilate one blob and fill it from its own surrounding ring (box-local,
    so the fill is this reflection's surroundings, not the whole eye's)."""
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
    """-> (cleaned, mask); input untouched. Crop coordinates.

    Automatic: small bright blobs inside the fitted ellipse x `search_scale`.
    Both guards matter for an unknown blob — the reach keeps off the lash line
    (masking it inflates the radius), `max_area` off the background.
    Pinned: no guards at all; the big stationary reflections are exactly the
    ones the automatic pass must be too timid to touch.
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
    """Radius of the bright blob clicked (or nearest within a few px), for
    sizing a pin; 8 px if nothing bright is there."""
    import cv2

    h, w = gray.shape
    x, y = int(round(at[0])), int(round(at[1]))
    if not (0 <= x < w and 0 <= y < h):
        return 8.0

    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        (gray >= threshold).astype(np.uint8), 8)
    lab = int(labels[y, x])
    if lab == 0:
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
