"""Where the camera's view lands on the DMD.

ROIs are camera px, masks are DMD mirrors; `DmdCalibration` holds the transform
between them. It is set by eye (`ReticlePose`, then an optional checkerboard
`Warp`, `reticle_align.py`): the stripe sweep and corner fits it replaced were
dropped, since scattering in the sample and relay erases the fine structure
they need (REFERENCE §5b).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

ON, OFF = np.uint8(255), np.uint8(0)


def apply_transform(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Map (N, 2) points through a 3×3 homogeneous matrix."""
    p = np.asarray(pts, dtype=np.float64)
    h = np.column_stack([p, np.ones(len(p))]) @ np.asarray(M).T
    w = np.where(np.abs(h[:, 2]) < 1e-12, 1e-12, h[:, 2])
    return h[:, :2] / w[:, None]


def _homog(M: np.ndarray, x, y) -> tuple[np.ndarray, np.ndarray]:
    """`apply_transform` on broadcastable x and y arrays."""
    den = M[2, 0] * x + M[2, 1] * y + M[2, 2]
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)
    return ((M[0, 0] * x + M[0, 1] * y + M[0, 2]) / den,
            (M[1, 0] * x + M[1, 1] * y + M[1, 2]) / den)


def _is_grid(x, y) -> bool:
    """x a row and y a column: a separable grid once broadcast."""
    return (np.ndim(y) == 2 and np.shape(y)[1] == 1
            and (np.ndim(x) == 1 or (np.ndim(x) == 2 and np.shape(x)[0] == 1)))


def _hat(nodes: np.ndarray, x: np.ndarray) -> np.ndarray:
    """(len(x), len(nodes)) linear-interpolation weights, held at the ends."""
    i = np.clip(np.searchsorted(nodes, x) - 1, 0, len(nodes) - 2)
    t = np.clip((x - nodes[i]) / (nodes[i + 1] - nodes[i]), 0.0, 1.0)
    W = np.zeros((len(x), len(nodes)))
    r = np.arange(len(x))
    W[r, i] = 1 - t
    W[r, i + 1] += t
    return W


def _every(a: np.ndarray, n: int) -> np.ndarray:
    """Every `n`th of ascending `a`, always with its last."""
    k = a[::n]
    return k if k[-1] == a[-1] else np.append(k, a[-1])


@dataclass(eq=False)
class Warp:
    """Bends the registration where one matrix can't follow the optics: a
    camera-px shift set at lattice nodes, bilinear between them and held at
    the edge value beyond. Camera→DMD becomes M·(p + shift(p)). Immutable:
    edits return a new Warp, so a calibration's mask cache stays valid."""
    xs: np.ndarray                    # (nx,) ascending node columns, camera px
    ys: np.ndarray                    # (ny,) ascending node rows
    dx: np.ndarray                    # (ny, nx) shift, camera px
    dy: np.ndarray

    def __post_init__(self) -> None:
        self.xs = np.asarray(self.xs, dtype=np.float64)
        self.ys = np.asarray(self.ys, dtype=np.float64)
        self.dx = np.asarray(self.dx, dtype=np.float64)
        self.dy = np.asarray(self.dy, dtype=np.float64)
        if (len(self.xs) < 2 or len(self.ys) < 2
                or self.dx.shape != (len(self.ys), len(self.xs))
                or self.dy.shape != self.dx.shape):
            raise ValueError("a warp needs ≥ 2×2 nodes and one shift per node")

    @classmethod
    def flat(cls, xs, ys) -> "Warp":
        shape = (len(ys), len(xs))
        return cls(xs, ys, np.zeros(shape), np.zeros(shape))

    @property
    def is_flat(self) -> bool:
        return not (self.dx.any() or self.dy.any())

    @property
    def nodes(self) -> np.ndarray:
        """(ny·nx, 2) node positions, row-major like `dx.flat`."""
        X, Y = np.meshgrid(self.xs, self.ys)
        return np.column_stack([X.ravel(), Y.ravel()])

    def moved(self, k: int, dx: float, dy: float) -> "Warp":
        """Node `k` (row-major) with its shift set to (dx, dy)."""
        nx, ny = self.dx.copy(), self.dy.copy()
        nx.flat[k], ny.flat[k] = dx, dy
        return Warp(self.xs, self.ys, nx, ny)

    def shift(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """The shift at broadcastable camera coordinates."""
        if _is_grid(x, y):      # two small matmuls: per-point was 1.8 s a frame
            Wx = _hat(self.xs, np.ravel(x).astype(np.float64))
            Wy = _hat(self.ys, np.ravel(y).astype(np.float64))
            return Wy @ self.dx @ Wx.T, Wy @ self.dy @ Wx.T
        x, y = np.broadcast_arrays(np.asarray(x, np.float64),
                                   np.asarray(y, np.float64))
        xs, ys = self.xs, self.ys
        i = np.clip(np.searchsorted(xs, x) - 1, 0, len(xs) - 2)
        j = np.clip(np.searchsorted(ys, y) - 1, 0, len(ys) - 2)
        tx = np.clip((x - xs[i]) / (xs[i + 1] - xs[i]), 0.0, 1.0)
        ty = np.clip((y - ys[j]) / (ys[j + 1] - ys[j]), 0.0, 1.0)

        def lerp(g):
            return ((1 - ty) * ((1 - tx) * g[j, i] + tx * g[j, i + 1])
                    + ty * ((1 - tx) * g[j + 1, i] + tx * g[j + 1, i + 1]))
        return lerp(self.dx), lerp(self.dy)

    def undo(self, u, v, *, tol: float = 0.05, iters: int = 8
             ) -> tuple[np.ndarray, np.ndarray]:
        """The p with p + shift(p) = (u, v). Fixed point: converges while a
        cell's shift changes by less than its size, true of any bend the
        light could follow."""
        x, y = u, v
        for _ in range(iters):
            sx, sy = self.shift(x, y)
            nx, ny = u - sx, v - sy
            done = (np.max(np.abs(nx - x), initial=0.0) < tol
                    and np.max(np.abs(ny - y), initial=0.0) < tol)
            x, y = nx, ny
            if done:
                break
        return x, y

    def resampled(self, xs, ys) -> "Warp":
        """The same bend on another lattice."""
        X, Y = np.meshgrid(np.asarray(xs, float), np.asarray(ys, float))
        dx, dy = self.shift(X, Y)
        return Warp(xs, ys, dx, dy)

    def to_dict(self) -> dict:
        return {"xs": self.xs.tolist(), "ys": self.ys.tolist(),
                "dx": self.dx.tolist(), "dy": self.dy.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Warp":
        return cls(d["xs"], d["ys"], d["dx"], d["dy"])


@dataclass
class DmdCalibration:
    """A DMD↔camera registration and its provenance."""
    cam_to_dmd: np.ndarray            # 3×3, camera px → DMD mirrors
    dmd_size:   tuple[int, int]       # (width, height) mirrors
    cam_size:   tuple[int, int]       # (width, height) px
    model:      str = "manual-reticle"
    created:    str = ""
    notes:      str = ""
    warp:       Warp | None = None    # applied before cam_to_dmd

    def __post_init__(self) -> None:
        # Safe to cache: every edit is a new object.
        self._mask_cache: dict[tuple[int, int], np.ndarray] = {}

    @property
    def dmd_to_cam(self) -> np.ndarray:
        """The matrix part only; `to_cam` includes the warp."""
        return np.linalg.inv(self.cam_to_dmd)

    def to_dmd_xy(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """Camera px → mirrors, on broadcastable arrays."""
        if self.warp is not None:
            sx, sy = self.warp.shift(x, y)
            x, y = x + sx, y + sy
        return _homog(np.asarray(self.cam_to_dmd, np.float64), x, y)

    _COARSE = 4                 # mirrors between exact unbends on a big grid

    def to_cam_xy(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """Mirrors → camera px, on broadcastable arrays. On a big row × column
        grid the bend is undone exactly every `_COARSE` mirrors and linear
        between: 1.6 camera px off at a cell edge under a 60 px bend, a
        third of a mirror; 316 → 60 ms a panel."""
        M = np.asarray(self.dmd_to_cam, np.float64)
        u, v = _homog(M, x, y)
        if self.warp is None:
            return u, v
        if not (_is_grid(x, y) and np.size(x) * np.size(y) > 4096):
            return self.warp.undo(u, v)
        xr = np.ravel(x).astype(np.float64)
        yc = np.ravel(y).astype(np.float64)
        xk, yk = _every(xr, self._COARSE), _every(yc, self._COARSE)
        uk, vk = _homog(M, xk[None, :], yk[:, None])
        pk, qk = self.warp.undo(uk, vk)
        Wx, Wy = _hat(xk, xr), _hat(yk, yc)
        return u + Wy @ (pk - uk) @ Wx.T, v + Wy @ (qk - vk) @ Wx.T

    def to_dmd(self, pts: np.ndarray) -> np.ndarray:
        p = np.atleast_2d(np.asarray(pts, np.float64))
        return np.column_stack(self.to_dmd_xy(p[:, 0], p[:, 1]))

    def to_cam(self, pts: np.ndarray) -> np.ndarray:
        p = np.atleast_2d(np.asarray(pts, np.float64))
        return np.column_stack(self.to_cam_xy(p[:, 0], p[:, 1]))

    # ── what the DMD can reach ──
    def accessible(self, pts: np.ndarray) -> np.ndarray:
        """Which camera points the DMD can illuminate."""
        d = self.to_dmd(pts)
        w, h = self.dmd_size
        return ((d[:, 0] >= 0) & (d[:, 0] <= w - 1)
                & (d[:, 1] >= 0) & (d[:, 1] <= h - 1))

    _MASK_ROWS = 256            # rows per band; caps the temporaries

    def accessible_mask(self, shape: tuple[int, int]) -> np.ndarray:
        """(H, W) reachable pixels. Cached and banded: the ROI editor asks on
        every drag, and the whole-grid version took 798 ms and ~1 GB."""
        h, w = int(shape[0]), int(shape[1])
        hit = self._mask_cache.get((h, w))
        if hit is not None:
            return hit

        dw, dh = self.dmd_size
        x = np.arange(w, dtype=np.float64)
        out = np.empty((h, w), dtype=bool)
        for y0 in range(0, h, self._MASK_ROWS):
            y = np.arange(y0, min(y0 + self._MASK_ROWS, h),
                          dtype=np.float64)[:, None]
            dx, dy = self.to_dmd_xy(x, y)
            out[y0:y0 + y.shape[0]] = ((dx >= 0) & (dx <= dw - 1)
                                       & (dy >= 0) & (dy <= dh - 1))
        out.flags.writeable = False         # shared
        self._mask_cache[(h, w)] = out
        return out

    def accessible_corners(self) -> np.ndarray:
        """The panel's four corners in camera px."""
        w, h = self.dmd_size
        return self.to_cam(
            np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float))

    def accessible_outline(self, per_side: int = 24) -> np.ndarray:
        """The panel's closed border in camera px; bent, if warped."""
        w, h = self.dmd_size
        t = np.linspace(0.0, 1.0, per_side, endpoint=False)
        x1, y1 = w - 1.0, h - 1.0
        edge = np.concatenate([
            np.column_stack([t * x1, np.zeros_like(t)]),
            np.column_stack([np.full_like(t, x1), t * y1]),
            np.column_stack([(1 - t) * x1, np.full_like(t, y1)]),
            np.column_stack([np.zeros_like(t), (1 - t) * y1]),
            [[0.0, 0.0]]])
        return self.to_cam(edge)

    # ── persistence ──
    def to_dict(self) -> dict:
        return {"cam_to_dmd": np.asarray(self.cam_to_dmd).tolist(),
                "dmd_size": list(self.dmd_size), "cam_size": list(self.cam_size),
                "model": self.model, "created": self.created,
                "notes": self.notes,
                "warp": None if self.warp is None else self.warp.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> "DmdCalibration":
        """Older files' fit statistics and vignette are ignored."""
        w = d.get("warp")
        return cls(cam_to_dmd=np.array(d["cam_to_dmd"], float),
                   dmd_size=tuple(d["dmd_size"]), cam_size=tuple(d["cam_size"]),
                   model=d.get("model", "affine"),
                   created=d.get("created", ""), notes=d.get("notes", ""),
                   warp=Warp.from_dict(w) if w else None)

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
                f"{self.cam_size[0]}x{self.cam_size[1]}, {self.model}"
                + (", bent" if self.warp is not None else "")
                + (f" ({self.created})" if self.created else ""))


# ── reticle alignment ─────────────────────────────────────────────────────────
# The DMD lights a reticle shape (camera px) through the operator's guess; once
# the light sits on the reticle drawn over the live camera, the guess is right.
RETICLE_KINDS = ("centre", "angle", "size")
RETICLE_DISC = 0.08                 # disc radius, × the camera's short side
RETICLE_ARMS = (0.8, 0.4, 0.12)     # L: +x arm, +y arm, thickness, × half that side
RETICLE_BOX = 0.6                   # box, × the camera frame


@dataclass(frozen=True)
class ReticlePose:
    """Camera→DMD as knobs: d = (cx, cy) + R(angle)·diag(sx, ±sy)·(p − centre).
    Mirrored negates sy; with a free angle that covers a flip on either axis."""
    cx: float                       # mirror under the camera's centre pixel
    cy: float
    angle_deg: float = 0.0
    sx: float = 1.0                 # mirrors per camera px
    sy: float = 1.0
    mirrored: bool = False

    def matrix(self, cam_size: tuple[int, int]) -> np.ndarray:
        t = np.radians(self.angle_deg)
        c, s = np.cos(t), np.sin(t)
        A = np.array([[c, -s], [s, c]]) @ np.diag(
            [self.sx, -self.sy if self.mirrored else self.sy])
        M = np.eye(3)
        M[:2, :2] = A
        M[:2, 2] = np.array([self.cx, self.cy]) - A @ _cam_centre(cam_size)
        return M

    @classmethod
    def centred(cls, dmd_size: tuple[int, int], cam_size: tuple[int, int],
                frac: float = 0.6) -> "ReticlePose":
        """A first guess: the panel upright and centred, `frac` of the camera
        frame at most on either side."""
        dw, dh = dmd_size
        cw, ch = cam_size
        s = max(dw / (frac * cw), dh / (frac * ch))
        return cls((dw - 1) / 2.0, (dh - 1) / 2.0, 0.0, s, s)

    @classmethod
    def from_calibration(cls, calib: DmdCalibration) -> "ReticlePose":
        """The nearest pose to a calibration's matrix: shear and perspective
        (an older fitted file's) are dropped, and its `warp` is not read."""
        M = np.asarray(calib.cam_to_dmd, dtype=np.float64)
        c = _cam_centre(calib.cam_size)
        cx, cy = apply_transform(M, c[None])[0]
        # The fit's Jacobian at the centre: a homography's local scale is not M/M22.
        A = (M[:2, :2] - np.outer([cx, cy], M[2, :2])) / (M[2, :2] @ c + M[2, 2])
        t = np.arctan2(A[1, 0], A[0, 0])
        y = -np.sin(t) * A[0, 1] + np.cos(t) * A[1, 1]     # (Rᵀ·A)[1, 1]
        return cls(float(cx), float(cy), float(np.degrees(t)),
                   float(np.hypot(A[0, 0], A[1, 0])), float(abs(y)), bool(y < 0))

    def to_calibration(self, dmd_size: tuple[int, int],
                       cam_size: tuple[int, int],
                       warp: Warp | None = None) -> DmdCalibration:
        """A flat `warp` is dropped."""
        if warp is not None and warp.is_flat:
            warp = None
        bend = ("" if warp is None else
                f"; bent at {len(warp.xs)}×{len(warp.ys)} checkerboard nodes")
        return DmdCalibration(
            cam_to_dmd=self.matrix(cam_size), dmd_size=tuple(dmd_size),
            cam_size=tuple(cam_size), model="manual-reticle",
            created=datetime.now().isoformat(timespec="seconds"),
            notes=f"aligned by eye to the camera reticle: {self}{bend}",
            warp=warp)


def _cam_centre(cam_size) -> np.ndarray:
    return np.array([(cam_size[0] - 1) / 2.0, (cam_size[1] - 1) / 2.0])


def _reticle_dims(cam_size, box_frac: float) -> dict:
    half = min(cam_size) / 2.0
    ax, ay, th = RETICLE_ARMS
    return {"r": RETICLE_DISC * 2 * half, "ax": ax * half, "ay": ay * half,
            "t": th * half / 2, "bw": box_frac * cam_size[0] / 2,
            "bh": box_frac * cam_size[1] / 2}


def reticle_outline(kind: str, cam_size: tuple[int, int], *,
                    box_frac: float = RETICLE_BOX) -> np.ndarray:
    """(N, 2) closed outline of `kind`'s shape in camera px."""
    d = _reticle_dims(cam_size, box_frac)
    if kind == "centre":
        a = np.linspace(0, 2 * np.pi, 97)
        rel = d["r"] * np.column_stack([np.cos(a), np.sin(a)])
    elif kind == "angle":
        t, ax, ay = d["t"], d["ax"], d["ay"]
        rel = np.array([(-t, -t), (ax, -t), (ax, t), (t, t), (t, ay),
                        (-t, ay), (-t, -t)])
    elif kind == "size":
        bw, bh = d["bw"], d["bh"]
        rel = np.array([(-bw, -bh), (bw, -bh), (bw, bh), (-bw, bh), (-bw, -bh)])
    else:
        raise ValueError(f"unknown reticle {kind!r}")
    return rel + _cam_centre(cam_size)


def reticle_frame(kind: str, pose: ReticlePose, dmd_size: tuple[int, int],
                  cam_size: tuple[int, int], *,
                  box_frac: float = RETICLE_BOX) -> np.ndarray:
    """The DMD frame that lights `reticle_outline(kind)` on the camera, if
    `pose` is right. Filled shapes: scattering leaves only coarse edges."""
    w, h = dmd_size
    inv = np.linalg.inv(pose.matrix(cam_size))
    X, Y = np.meshgrid(np.arange(w, dtype=np.float32),
                       np.arange(h, dtype=np.float32))
    c = _cam_centre(cam_size)
    u = inv[0, 0] * X + inv[0, 1] * Y + (inv[0, 2] - c[0])
    v = inv[1, 0] * X + inv[1, 1] * Y + (inv[1, 2] - c[1])
    d = _reticle_dims(cam_size, box_frac)
    if kind == "centre":
        lit = u * u + v * v <= d["r"] ** 2
    elif kind == "angle":
        t = d["t"]
        lit = (((u >= -t) & (u <= d["ax"]) & (np.abs(v) <= t))
               | ((v >= -t) & (v <= d["ay"]) & (np.abs(u) <= t)))
    elif kind == "size":
        lit = (np.abs(u) <= d["bw"]) & (np.abs(v) <= d["bh"])
    else:
        raise ValueError(f"unknown reticle {kind!r}")
    return np.where(lit, ON, OFF)


# ── bending ───────────────────────────────────────────────────────────────────
# After the pose, a checkerboard over the reticle box shows what one matrix
# can't: shear, keystone and lens bend. Its square corners are the `Warp` nodes.
CHECKER_COLS = 6


def checker_lattice(cam_size: tuple[int, int], cols: int = CHECKER_COLS, *,
                    box_frac: float = RETICLE_BOX
                    ) -> tuple[np.ndarray, np.ndarray]:
    """The board's square corners (xs, ys) in camera px: `cols` squares
    across the reticle box, as many rows as keeps them near square."""
    d = _reticle_dims(cam_size, box_frac)
    rows = max(1, round(cols * d["bh"] / d["bw"]))
    c = _cam_centre(cam_size)
    return (np.linspace(c[0] - d["bw"], c[0] + d["bw"], cols + 1),
            np.linspace(c[1] - d["bh"], c[1] + d["bh"], rows + 1))


def checker_frame(calib: DmdCalibration, xs: np.ndarray,
                  ys: np.ndarray) -> np.ndarray:
    """The DMD frame that lights a checkerboard with square corners at
    `xs` × `ys` (camera px, ascending), if `calib` is right; dark off the
    board. Asks each mirror where it lands, as `RoiSet.dmd_frame` does."""
    w, h = calib.dmd_size
    u, v = calib.to_cam_xy(np.arange(w, dtype=np.float64)[None, :],
                           np.arange(h, dtype=np.float64)[:, None])
    i = np.searchsorted(xs, u, side="right") - 1
    j = np.searchsorted(ys, v, side="right") - 1
    lit = ((i >= 0) & (i < len(xs) - 1) & (j >= 0) & (j < len(ys) - 1)
           & ((i + j) % 2 == 0))
    return np.where(lit, ON, OFF)
