"""XY stage — controller that OWNS the connection to whichever backend is
connected (see backend.py).

Only one program can hold the controller's port, so a single StageController is
the sole device owner. The polling worker (on its thread) and the GUI motion
buttons both call it, sharing one connection — serialized by a lock in the
MCM6101 driver; the MCM301 driver has none (see mcm301_driver.py).

Motion methods take and return MICRONS, and soft limits clamp every target.
Nothing moves unless `jog_um` / `move_to_um` is called.
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


def _rotate_jog(which: str, delta_um: float, deg: float) -> tuple[float, float]:
    """A jog request along the LOGICAL (camera-aligned) `which` axis, resolved
    into physical (dx, dy) deltas for the stage's own X/Y — see
    `StageSettings.frame_rotation_deg`. `deg == 0` short-circuits to the exact
    single-axis delta (no trig, no float drift), matching jog_um's pre-
    rotation behaviour exactly when the operator hasn't set a rotation."""
    dx, dy = (delta_um, 0.0) if which == "x" else (0.0, delta_um)
    if deg == 0.0:
        return dx, dy
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return dx * c - dy * s, dx * s + dy * c


def _center_here_updates(s: StageSettings, cx: int, cy: int) -> dict[int, dict]:
    """Shared by StageController and MockStageController: compute the
    origin/soft-limit updates for both axes and fold them into the live axis
    objects. Persisting (or not) is left to the caller."""
    updates = {s.x.index: s.x.center_updates(cx, s.margin_um),
               s.y.index: s.y.center_updates(cy, s.margin_um)}
    for ax, upd in ((s.x, updates[s.x.index]), (s.y, updates[s.y.index])):
        ax.apply_updates(upd)
    return updates


class StageController:
    def __init__(self, settings: StageSettings):
        self._s = settings
        self._dev = None
        self.backend_kind: str | None = None   # which driver connect() picked; see backend.py

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
        # The calibrated command→encoder map, so absolute moves land right.
        # (A no-op on backends with no such scale, e.g. the MCM301.)
        axes = (self._s.x, self._s.y, self._s.z) if self._s.z else (self._s.x, self._s.y)
        for ax in axes:
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

    @property
    def has_z(self) -> bool:
        return self._s.has_z

    @property
    def supports_reframe(self) -> bool:
        """Whether the connected backend can drive a hard-limit frame
        re-establish (see establish_frame below) -- False on the
        MCM301, whose position readout is already a stable encoder count and
        never drifts. CalibrationDialog checks this for the Z button only
        (the X/Y "Re-establish frame…" button always shows; on a backend
        without the method it just refuses with a clear message)."""
        return self._dev is not None and hasattr(self._dev, "establish_frame")

    # ── hard-limit detection ─────────────────────────────────────────────────
    def _note_limit(self, ax: StageAxis, status) -> None:
        """Latch `ax.frame_stale` the moment a hard-limit status bit is seen.

        Only meaningful on a backend whose command origin can actually drift
        on a limit hit (`supports_reframe` — the MCM301's readout never does,
        see its own docstring); checking here regardless would be harmless
        but misleading, flagging a staleness that backend can't have.
        Sticky on purpose — the corruption started the instant the limit was
        touched and persists after backing off it, so this must not clear
        itself just because the bit reads False again next poll. Only a fresh
        `establish_frame()` (StageAxis.apply_updates) clears it.
        """
        if self.supports_reframe and (status.at_fwd_limit or status.at_rev_limit):
            ax.frame_stale = True

    # ── reads ───────────────────────────────────────────────────────────────
    def read_xy_um(self) -> tuple[float, float]:
        if self._dev is None:
            raise StageControllerError("not connected")
        sx = self._dev.get_status(self._s.x.index)
        sy = self._dev.get_status(self._s.y.index)
        self._note_limit(self._s.x, sx)
        self._note_limit(self._s.y, sy)
        return (self._s.x.to_um(sx.position), self._s.y.to_um(sy.position))

    def read_z_um(self) -> float:
        """Focus position. Raises on a rig with no Z stage — callers gate on
        `settings.has_z` first, same as every other Z entry point here."""
        if self._dev is None:
            raise StageControllerError("not connected")
        if self._s.z is None:
            raise StageControllerError("this rig has no Z stage")
        sz = self._dev.get_status(self._s.z.index)
        self._note_limit(self._s.z, sz)
        return self._s.z.to_um(sz.position)

    # ── motion (physically moves the stage) ─────────────────────────────────
    def move_to_um(self, which: str, target_um: float) -> None:
        if self._dev is None:
            raise StageControllerError("not connected")
        ax = self._axis(which)
        if not ax.has_frame:
            raise StageControllerError(
                f"{ax.name}: no valid frame (never calibrated, or invalidated "
                f"by a hard-limit hit since) — absolute moves are refused "
                f"until it's re-established. Jog instead, or use "
                f"Calibrate… -> Re-establish frame.")
        counts = ax.clamp_counts(ax.um_to_counts(target_um))
        self._dev.move_to_readout(ax.index, counts)

    def jog_um(self, which: str, delta_um: float) -> None:
        if self._dev is None:
            raise StageControllerError("not connected")
        if which == "z":
            # Focus isn't part of the camera-aligned XY plane, so
            # frame_rotation_deg (an X/Y-only display setting) never applies —
            # the same single-axis move _rotate_jog degenerates to at deg==0.
            ax = self._axis("z")
            st = self._dev.get_status(ax.index)
            self._note_limit(ax, st)
            if ax.frame_stale:
                raise StageControllerError(
                    f"{ax.name}: a hard limit was hit since the last "
                    f"calibration — the command map is unreliable, so even a "
                    f"jog can land somewhere else. Re-establish the frame "
                    f"before moving further.")
            cur = st.position
            target = ax.clamp_counts(
                int(round(cur + ax.sign * delta_um * ax.counts_per_um)))
            self._dev.move_to_readout(ax.index, target)
            return
        dx, dy = _rotate_jog(which, delta_um, self._s.frame_rotation_deg)
        for ax, d in ((self._s.x, dx), (self._s.y, dy)):
            if not d:
                continue
            st = self._dev.get_status(ax.index)
            self._note_limit(ax, st)
            if ax.frame_stale:
                raise StageControllerError(
                    f"{ax.name}: a hard limit was hit since the last "
                    f"calibration — the command map is unreliable, so even a "
                    f"jog can land somewhere else. Re-establish the frame "
                    f"before moving further.")
            cur = st.position
            target = ax.clamp_counts(int(round(cur + ax.sign * d * ax.counts_per_um)))
            self._dev.move_to_readout(ax.index, target)

    def stop(self, which: str) -> None:
        if self._dev is not None:
            self._dev.stop(self._axis(which).index)

    def stop_all(self) -> None:
        if self._dev is not None:
            idxs = [self._s.x.index, self._s.y.index]
            if self._s.z is not None:
                idxs.append(self._s.z.index)
            self._dev.stop_all(idxs)

    # ── frame / origin calibration ──────────────────────────────────────────
    # A HARD LIMIT hit re-references the controller's command origin, which
    # invalidates slope/offset — absolute go-to lands wrong, jog still works.
    #   set_center_here()  — no motion; declares "here" as 0,0.
    #   establish_frame()  — MOTION: drives to the reverse limit and re-measures.

    def read_xy_counts(self) -> tuple[int, int]:
        """Raw encoder counts (no origin applied) — calibration works here."""
        if self._dev is None:
            raise StageControllerError("not connected")
        return (self._dev.get_status(self._s.x.index).position,
                self._dev.get_status(self._s.y.index).position)

    def set_center_here(self) -> dict[int, dict]:
        """Define the CURRENT position as 0,0 and put soft limits at ±½ inch
        around it (keeps the stage inside the encoder's no-wrap zone). Does NOT
        move the stage — centre it first. Persists to the shared config."""
        cx, cy = self.read_xy_counts()
        updates = _center_here_updates(self._s, cx, cy)
        save_axis_updates(updates)
        return updates

    def set_z_zero_here(self) -> dict[int, dict]:
        """Z's own zero — deliberately separate from set_center_here(): an
        operator centering X/Y in the field of view has nothing to do with
        where the focus knob happens to be, so the two must never be coupled.
        No motion. Soft/travel limits land at ±half the stage's rated 25.4 mm
        travel (StageAxis.center_updates, the same math set_center_here() uses
        for X/Y) around wherever Z is right now."""
        if self._dev is None:
            raise StageControllerError("not connected")
        if self._s.z is None:
            raise StageControllerError("this rig has no Z stage")
        cz = self._dev.get_status(self._s.z.index).position
        updates = {self._s.z.index: self._s.z.center_updates(cz, self._s.margin_um)}
        self._s.z.apply_updates(updates[self._s.z.index])
        save_axis_updates(updates)
        return updates

    def establish_frame(self, progress=None,
                        axes: tuple[str, ...] = ("x", "y")) -> dict[int, dict]:
        """MOTION — drives each of `axes` into its REVERSE hard limit, then
        probes twice to re-measure `enc = slope·cmd + offset`, restoring
        absolute positioning after a limit hit.

        `axes` defaults to X/Y only — Z is opt-in via `axes=("z",)` and is a
        SEPARATE call from the caller's side on purpose: Z is a focus axis, and
        the UI trigger for it carries its own, much stronger warnings (this
        method carries no awareness of what's mounted on the stage — that
        judgment call belongs entirely to the caller). Requesting "z" on a rig
        with none configured raises before anything moves.

        Never invents an origin: the driver's geometric centre isn't where
        anyone wants 0,0 here, and overwriting a user-set one silently moves the
        coordinate system. 0,0 comes from `set_center_here()`/`set_z_zero_here()`
        only.

        An existing origin is KEPT if it still falls inside the freshly measured
        travel, DROPPED if not — a limit hit can re-reference the encoder too,
        leaving `true_center` pointing at nothing. Dropping forces a deliberate
        re-zero rather than quietly wrong microns.

        Blocks for a minute or more per axis — call it off the GUI thread.
        """
        if self._dev is None:
            raise StageControllerError("not connected")
        if "z" in axes and self._s.z is None:
            raise StageControllerError("this rig has no Z stage")
        if not hasattr(self._dev, "establish_frame"):
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
            res = self._dev.establish_frame(ax.index, ax.default_span())
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
        """MOTION: absolute move both axes to 0,0 (the true centre)."""
        if self._dev is None:
            raise StageControllerError("not connected")
        for ax in (self._s.x, self._s.y):
            if not ax.has_frame:
                raise StageControllerError(
                    f"{ax.name}: no valid frame — see move_to_um.")
            self._dev.move_to_readout(ax.index, int(round(ax.ref_counts)))

    # ── session home (a bookmark, not calibration) ──────────────────────────
    # In memory only, never written to the config: "the spot I'm working at
    # today" must not outlive the sample, nor be confused with the true zero.

    def set_home_here(self) -> tuple[float, float]:
        """Bookmark the current position as this session's home. No motion."""
        cx, cy = self.read_xy_counts()
        self._s.x.home_counts, self._s.y.home_counts = cx, cy
        return (self._s.x.to_um(cx), self._s.y.to_um(cy))

    def clear_home(self) -> None:
        self._s.x.home_counts = self._s.y.home_counts = None

    def go_home(self) -> None:
        """MOTION: absolute move both axes back to the session home.

        `home_counts` is a raw encoder count captured fresh by
        `set_home_here()`, independent of origin/`ref_counts` — so this only
        needs the command map to still be trustworthy (`frame_stale`), not
        the fuller `has_frame` `move_to_um` requires (home works even before
        an axis has ever been zeroed, same as jog)."""
        if self._dev is None:
            raise StageControllerError("not connected")
        if self._s.x.home_counts is None or self._s.y.home_counts is None:
            raise StageControllerError("no home set this session")
        for ax in (self._s.x, self._s.y):
            if ax.frame_stale:
                raise StageControllerError(
                    f"{ax.name}: a hard limit was hit since the last "
                    f"calibration — the command map is unreliable. "
                    f"Re-establish the frame before moving further.")
            self._dev.move_to_readout(ax.index, ax.clamp_counts(int(ax.home_counts)))


class MockStageController:
    """Simulated stage: position eases toward the last commanded target."""
    _STEP_UM = 300.0        # max µm moved per read (visible motion at poll rate)

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
        """The mock always implements establish_frame (it's what lets the
        rest of the app be tested without real hardware), so it always
        reports support -- matching StageController's real capability check
        would require a fake backend object with no establish_frame, which
        no test needs."""
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
        # advance current toward target by up to _STEP_UM per read
        for k in ("x", "y"):
            self._ease(k)
        return (self._pos["x"], self._pos["y"])

    def read_z_um(self) -> float:
        if not self._s.has_z:
            raise StageControllerError("this rig has no Z stage")
        self._ease("z")
        return self._pos["z"]

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
            if not d:
                continue
            self._target[k] = self._clamp_um(k, self._pos[k] + d)

    def stop(self, which: str) -> None:
        self._target[which] = self._pos[which]

    def stop_all(self) -> None:
        self._target = dict(self._pos)

    # ── frame / origin calibration (simulated) ──────────────────────────────
    # These deliberately DO NOT write the shared config: a calibration produced
    # with no stage attached would overwrite the real one with fiction.

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
        return updates      # simulated: never written to disk, like X/Y's mock

    def establish_frame(self, progress=None,
                        axes: tuple[str, ...] = ("x", "y")) -> dict[int, dict]:
        """Slope/offset only — same contract as the real one: the origin and the
        soft limits are left alone. See StageController.establish_frame for
        why Z is opt-in via `axes`."""
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
