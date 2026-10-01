"""XY(Z) stage controller: the sole owner of the backend connection (see
backend.py), since only one program can hold the port.

The poll worker's thread and the GUI's motion buttons share that one
connection. The MCM6101 driver serializes it with a lock; the MCM301 driver
has none.

Motion methods take and return MICRONS; soft limits clamp every target.
"""
from __future__ import annotations

import math

from .settings import StageAxis, StageSettings, save_axis_updates


class StageControllerError(Exception):
    pass


def _pick_axis(s: StageSettings, which: str) -> StageAxis:
    if which == "x":
        return s.x
    if which == "y":
        return s.y
    if s.z is None:
        raise StageControllerError("this rig has no Z stage")
    return s.z


def _active_axes(s: StageSettings) -> tuple[StageAxis, ...]:
    return (s.x, s.y, s.z) if s.z is not None else (s.x, s.y)


def _rotate_jog(which: str, delta_um: float, deg: float) -> tuple[float, float]:
    """A jog along the camera-aligned `which` axis -> the stage's own (dx, dy)
    (`StageSettings.frame_rotation_deg`). deg == 0 is exact: no trig, no
    float drift."""
    dx, dy = (delta_um, 0.0) if which == "x" else (0.0, delta_um)
    if deg == 0.0:
        return dx, dy
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return dx * c - dy * s, dx * s + dy * c


def _center_here_updates(s: StageSettings, cx: int, cy: int) -> dict[int, dict]:
    """X/Y origin and soft-limit updates, applied to the live axes; the
    caller decides whether to persist."""
    updates = {s.x.index: s.x.center_updates(cx, s.margin_um),
               s.y.index: s.y.center_updates(cy, s.margin_um)}
    for ax, upd in ((s.x, updates[s.x.index]), (s.y, updates[s.y.index])):
        ax.apply_updates(upd)
    return updates


class StageController:
    def __init__(self, settings: StageSettings):
        self._s = settings
        self._dev = None
        self.backend_kind: str | None = None   # which driver connect() picked

    # ── connection ──────────────────────────────────────────────────────────
    def connect(self) -> None:
        from .backend import BackendError, connect_auto, open_backend
        kind = self._s.controller or "auto"
        try:
            if kind == "auto":
                kind, dev = connect_auto(self._s.port)
            else:
                dev = open_backend(kind, self._s.port)
        except BackendError as e:
            raise StageControllerError(str(e)) from e
        # The calibrated command->encoder map (a no-op on the MCM301).
        for ax in _active_axes(self._s):
            if ax.slope is not None and ax.offset is not None:
                dev.set_linear_map(ax.index, ax.slope, ax.offset)
        self._dev = dev
        self.backend_kind = kind

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None
                self.backend_kind = None

    def _axis(self, which: str) -> StageAxis:
        return _pick_axis(self._s, which)

    def _connected(self):
        if self._dev is None:
            raise StageControllerError("not connected")
        return self._dev

    @property
    def has_z(self) -> bool:
        return self._s.has_z

    @property
    def supports_reframe(self) -> bool:
        """Whether the backend has establish_frame (False on the MCM301,
        whose readout never drifts). CalibrationDialog gates only the Z
        button on this; the X/Y one refuses with a message instead."""
        return self._dev is not None and hasattr(self._dev, "establish_frame")

    # ── hard-limit detection ─────────────────────────────────────────────────
    def _note_limit(self, ax: StageAxis, status) -> None:
        """Latch `ax.frame_stale` on a hard-limit bit, only where the origin
        can drift (`supports_reframe`). Sticky: the map stays wrong after
        backing off the limit; only establish_frame() clears it."""
        if self.supports_reframe and (status.at_fwd_limit or status.at_rev_limit):
            ax.frame_stale = True

    # ── reads ───────────────────────────────────────────────────────────────
    def read_xy_um(self) -> tuple[float, float]:
        dev = self._connected()
        sx = dev.get_status(self._s.x.index)
        sy = dev.get_status(self._s.y.index)
        self._note_limit(self._s.x, sx)
        self._note_limit(self._s.y, sy)
        return (self._s.x.to_um(sx.position), self._s.y.to_um(sy.position))

    def read_z_um(self) -> float:
        """Raises on a rig with no Z; callers gate on `settings.has_z`."""
        dev = self._connected()
        z = self._axis("z")
        sz = dev.get_status(z.index)
        self._note_limit(z, sz)
        return z.to_um(sz.position)

    def is_moving(self) -> bool:
        dev = self._connected()
        return any(dev.get_status(ax.index).moving
                   for ax in _active_axes(self._s))

    # ── motion (physically moves the stage) ─────────────────────────────────
    def move_to_um(self, which: str, target_um: float) -> None:
        dev = self._connected()
        ax = self._axis(which)
        if not ax.has_frame:
            raise StageControllerError(
                f"{ax.name}: no valid frame (never calibrated, or invalidated "
                f"by a hard-limit hit since) — absolute moves are refused "
                f"until it's re-established. Jog instead, or use "
                f"Calibrate… -> Re-establish frame.")
        counts = ax.clamp_counts(ax.um_to_counts(target_um))
        dev.move_to_readout(ax.index, counts)

    def _jog_axis(self, dev, ax: StageAxis, delta_um: float) -> None:
        st = dev.get_status(ax.index)
        self._note_limit(ax, st)
        if ax.frame_stale:
            raise StageControllerError(
                f"{ax.name}: a hard limit was hit since the last "
                f"calibration — the command map is unreliable, so even a "
                f"jog can land somewhere else. Re-establish the frame "
                f"before moving further.")
        dev.move_to_readout(ax.index, ax.clamp_counts(
            int(round(st.position + ax.sign * delta_um * ax.counts_per_um))))

    def jog_um(self, which: str, delta_um: float) -> None:
        dev = self._connected()
        if which == "z":        # focus: frame_rotation_deg is X/Y only
            self._jog_axis(dev, self._axis("z"), delta_um)
            return
        dx, dy = _rotate_jog(which, delta_um, self._s.frame_rotation_deg)
        for ax, d in ((self._s.x, dx), (self._s.y, dy)):
            if d:
                self._jog_axis(dev, ax, d)

    def stop(self, which: str) -> None:
        if self._dev is not None:
            self._dev.stop(self._axis(which).index)

    def stop_all(self) -> None:
        if self._dev is not None:
            self._dev.stop_all([ax.index for ax in _active_axes(self._s)])

    # ── frame / origin calibration ──────────────────────────────────────────
    # A HARD LIMIT hit re-references the MCM6101's command origin, which
    # invalidates slope/offset: absolute go-to lands wrong, jog still works.

    def read_xy_counts(self) -> tuple[int, int]:
        """Raw encoder counts, no origin applied."""
        dev = self._connected()
        return (dev.get_status(self._s.x.index).position,
                dev.get_status(self._s.y.index).position)

    def set_center_here(self) -> dict[int, dict]:
        """No motion: the current position becomes 0,0, soft limits ±½ inch
        around it (inside the encoder's no-wrap zone). Persisted."""
        cx, cy = self.read_xy_counts()
        updates = _center_here_updates(self._s, cx, cy)
        save_axis_updates(updates)
        return updates

    def set_z_zero_here(self) -> dict[int, dict]:
        """No motion: Z's own zero, never coupled to X/Y's 0,0. Limits land
        at ±half the rated 25.4 mm travel around the current Z. Persisted."""
        dev = self._connected()
        z = self._axis("z")
        cz = dev.get_status(z.index).position
        updates = {z.index: z.center_updates(cz, self._s.margin_um)}
        z.apply_updates(updates[z.index])
        save_axis_updates(updates)
        return updates

    def establish_frame(self, progress=None,
                        axes: tuple[str, ...] = ("x", "y")) -> dict[int, dict]:
        """MOTION: drive each of `axes` into its REVERSE hard limit and
        re-measure `enc = slope·cmd + offset`. Blocks a minute or more per
        axis; call it off the GUI thread.

        Z is opt-in (`axes=("z",)`), a separate call on purpose: it runs
        under the objective, and the caller owns those warnings. Asking for Z
        on a rig without one raises before anything moves.

        Never invents an origin (0,0 comes only from set_center_here /
        set_z_zero_here). An existing one is KEPT if inside the measured
        travel, else DROPPED: a limit hit can re-reference the encoder too,
        and a forced re-zero beats quietly wrong microns.
        """
        dev = self._connected()
        if "z" in axes and self._s.z is None:
            raise StageControllerError("this rig has no Z stage")
        if not hasattr(dev, "establish_frame"):
            raise StageControllerError(
                f"The {self.backend_kind} backend has no drifting command "
                "origin to re-establish — its position readout is already a "
                "stable encoder count. Use 'Set 0,0 = center' / 'Set Z = 0' "
                "instead.")

        def say(msg: str) -> None:
            if progress is not None:
                progress(msg)

        updates: dict[int, dict] = {}
        dropped: list[str] = []
        for ax in (self._axis(a) for a in axes):
            say(f"{ax.name}: driving to reverse limit…")
            res = dev.establish_frame(ax.index, ax.default_span())
            lo, hi = int(res["travel_min"]), int(res["travel_max"])
            upd = {"slope":  round(float(res["slope"]), 5),
                   "offset": round(float(res["offset"]), 3)}

            if ax.origin_set and lo <= ax.ref_counts <= hi:
                note = f"origin kept at {ax.ref_counts:.0f}"
            else:
                # Stale or absent: park soft limits on the measured travel and
                # leave the origin unset, so the UI blocks absolute go-to.
                margin = int(round(self._s.margin_um * ax.counts_per_um))
                upd.update({"true_center": None,
                            "travel_min": lo, "travel_max": hi,
                            "soft_min": lo + margin, "soft_max": hi - margin})
                note = (f"origin {ax.ref_counts:.0f} is outside the measured "
                        f"travel [{lo}, {hi}] — dropped" if ax.origin_set
                        else "no origin set")
                dropped.append(ax.name)

            ax.apply_updates(upd)
            updates[ax.index] = upd
            say(f"{ax.name}: slope {upd['slope']:.4f}, offset {upd['offset']:.1f}, "
                f"travel [{lo}, {hi}]; {note}")

        save_axis_updates(updates)
        say("Frame re-established. " + (
            f"0,0 not set for {', '.join(dropped)} — centre the stage and click "
            "'Set 0,0 = center (here)'." if dropped else "Origin unchanged."))
        return updates

    def go_to_center(self) -> None:
        """MOTION: absolute move of X and Y to 0,0."""
        dev = self._connected()
        for ax in (self._s.x, self._s.y):
            if not ax.has_frame:
                raise StageControllerError(
                    f"{ax.name}: no valid frame — see move_to_um.")
            dev.move_to_readout(ax.index, int(round(ax.ref_counts)))

    # ── session home (a bookmark, not calibration) ──────────────────────────
    # Never persisted: it must not outlive the sample or pass for the zero.

    def set_home_here(self) -> tuple[float, float]:
        """Bookmark the current position as this session's home. No motion."""
        cx, cy = self.read_xy_counts()
        self._s.x.home_counts, self._s.y.home_counts = cx, cy
        return (self._s.x.to_um(cx), self._s.y.to_um(cy))

    def clear_home(self) -> None:
        self._s.x.home_counts = self._s.y.home_counts = None

    def go_home(self) -> None:
        """MOTION: absolute move of X and Y to the session home. Home is raw
        counts, independent of the origin, so like jog it needs only a
        non-stale map, not `has_frame`."""
        dev = self._connected()
        if self._s.x.home_counts is None or self._s.y.home_counts is None:
            raise StageControllerError("no home set this session")
        for ax in (self._s.x, self._s.y):
            if ax.frame_stale:
                raise StageControllerError(
                    f"{ax.name}: a hard limit was hit since the last "
                    f"calibration — the command map is unreliable. "
                    f"Re-establish the frame before moving further.")
            dev.move_to_readout(ax.index, ax.clamp_counts(int(ax.home_counts)))


class MockStageController:
    """Simulated stage: position eases toward the last commanded target."""
    _STEP_UM = 300.0        # max µm per read: visible motion at poll rate

    backend_kind = "mock"

    def __init__(self, settings: StageSettings):
        self._s = settings
        axes = ("x", "y", "z") if settings.has_z else ("x", "y")
        self._pos = {k: 0.0 for k in axes}
        self._target = {k: 0.0 for k in axes}
        self._open = False

    def connect(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _axis(self, which: str) -> StageAxis:
        return _pick_axis(self._s, which)

    @property
    def has_z(self) -> bool:
        return self._s.has_z

    @property
    def supports_reframe(self) -> bool:
        """The mock always implements establish_frame."""
        return True

    def _clamp_um(self, which: str, um: float) -> float:
        lo, hi = self._axis(which).soft_limits_um()
        return max(lo, min(hi, um))

    def _ease(self, k: str) -> None:
        d = self._target[k] - self._pos[k]
        if abs(d) <= self._STEP_UM:
            self._pos[k] = self._target[k]
        else:
            self._pos[k] += self._STEP_UM * (1 if d > 0 else -1)

    def read_xy_um(self) -> tuple[float, float]:
        for k in ("x", "y"):
            self._ease(k)
        return (self._pos["x"], self._pos["y"])

    def read_z_um(self) -> float:
        if not self._s.has_z:
            raise StageControllerError("this rig has no Z stage")
        self._ease("z")
        return self._pos["z"]

    def is_moving(self) -> bool:
        for k in self._pos:
            self._ease(k)
        return any(self._pos[k] != self._target[k] for k in self._pos)

    def move_to_um(self, which: str, target_um: float) -> None:
        ax = self._axis(which)
        if not ax.has_frame:
            raise StageControllerError(
                f"{ax.name}: no valid frame — see StageController.move_to_um.")
        self._target[which] = self._clamp_um(which, target_um)

    def jog_um(self, which: str, delta_um: float) -> None:
        if which == "z":     # not part of the XY plane — see StageController
            if self._axis("z").frame_stale:
                raise StageControllerError(
                    "Z: a hard limit was hit since the last calibration — "
                    "see StageController.jog_um.")
            self._target["z"] = self._clamp_um("z", self._pos["z"] + delta_um)
            return
        for k in ("x", "y"):
            if self._axis(k).frame_stale:
                raise StageControllerError(
                    f"{k.upper()}: a hard limit was hit since the last "
                    f"calibration — see StageController.jog_um.")
        dx, dy = _rotate_jog(which, delta_um, self._s.frame_rotation_deg)
        for k, d in (("x", dx), ("y", dy)):
            if d:
                self._target[k] = self._clamp_um(k, self._pos[k] + d)

    def stop(self, which: str) -> None:
        self._target[which] = self._pos[which]

    def stop_all(self) -> None:
        self._target = dict(self._pos)

    # ── frame / origin calibration (simulated) ──────────────────────────────
    # Never written to the shared config: it would overwrite the real
    # calibration with fiction.

    def read_xy_counts(self) -> tuple[int, int]:
        return (self._s.x.um_to_counts(self._pos["x"]),
                self._s.y.um_to_counts(self._pos["y"]))

    def set_center_here(self) -> dict[int, dict]:
        cx, cy = self.read_xy_counts()
        updates = _center_here_updates(self._s, cx, cy)
        z = {"z": self._pos["z"]} if self._s.has_z else {}
        self._pos = {"x": 0.0, "y": 0.0, **z}   # "here" is now the origin
        self._target = dict(self._pos)
        return updates

    def set_z_zero_here(self) -> dict[int, dict]:
        if not self._s.has_z:
            raise StageControllerError("this rig has no Z stage")
        cz = self._s.z.um_to_counts(self._pos["z"])
        updates = {self._s.z.index: self._s.z.center_updates(cz, self._s.margin_um)}
        self._s.z.apply_updates(updates[self._s.z.index])
        self._pos["z"] = self._target["z"] = 0.0   # "here" is now Z's origin
        return updates

    def establish_frame(self, progress=None,
                        axes: tuple[str, ...] = ("x", "y")) -> dict[int, dict]:
        """Slope/offset only; origin and soft limits untouched."""
        import time
        if "z" in axes and not self._s.has_z:
            raise StageControllerError("this rig has no Z stage")
        updates: dict[int, dict] = {}
        for ax in (self._axis(a) for a in axes):
            if progress is not None:
                progress(f"{ax.name}: driving to reverse limit… (simulated)")
            time.sleep(0.4)
            upd = {"slope": 17.778, "offset": 0.0}
            ax.apply_updates(upd)
            updates[ax.index] = upd
        if progress is not None:
            progress("Frame re-established (simulated; nothing saved).")
        return updates

    def go_to_center(self) -> None:
        for ax in (self._s.x, self._s.y):
            if not ax.has_frame:
                raise StageControllerError(
                    f"{ax.name}: no valid frame — see StageController.go_to_center.")
        self._target = {"x": 0.0, "y": 0.0}

    def set_home_here(self) -> tuple[float, float]:
        self._s.x.home_counts, self._s.y.home_counts = self.read_xy_counts()
        return (self._pos["x"], self._pos["y"])

    def clear_home(self) -> None:
        self._s.x.home_counts = self._s.y.home_counts = None

    def go_home(self) -> None:
        if self._s.x.home_counts is None or self._s.y.home_counts is None:
            raise RuntimeError("no home set this session")
        for k, ax in (("x", self._s.x), ("y", self._s.y)):
            if ax.frame_stale:
                raise StageControllerError(
                    f"{ax.name}: a hard limit was hit since the last "
                    f"calibration — see StageController.go_home.")
            self._target[k] = self._clamp_um(k, ax.to_um(ax.home_counts))
