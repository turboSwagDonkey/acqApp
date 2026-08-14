"""XY stage — controller that OWNS the serial connection.

Only one program can hold the MCM6101's port, so a single StageController is the
sole device owner. The polling worker (on its thread) and the GUI motion buttons
both call it; the driver is lock-guarded, so reads and motion commands serialize
safely on one connection.

Motion methods take and return MICRONS, and soft limits clamp every target.
Nothing moves unless `jog_um` / `move_to_um` is called.
"""
from __future__ import annotations

from .settings import StageAxis, StageSettings, save_axis_updates


class StageControllerError(Exception):
    pass


class StageController:
    def __init__(self, settings: StageSettings):
        self._s = settings
        self._dev = None

    # ── connection ──────────────────────────────────────────────────────────
    def connect(self) -> None:
        from .driver import MCM6101
        dev = MCM6101(self._s.port)
        dev.open()
        # The calibrated command→encoder map, so absolute moves land right.
        for ax in (self._s.x, self._s.y):
            if ax.slope is not None and ax.offset is not None:
                dev.set_linear_map(ax.index, ax.slope, ax.offset)
        self._dev = dev

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    def _axis(self, which: str) -> StageAxis:
        return self._s.x if which == "x" else self._s.y

    # ── reads ───────────────────────────────────────────────────────────────
    def read_xy_um(self) -> tuple[float, float]:
        if self._dev is None:
            raise StageControllerError("not connected")
        sx = self._dev.get_status(self._s.x.index)
        sy = self._dev.get_status(self._s.y.index)
        return (self._s.x.to_um(sx.position), self._s.y.to_um(sy.position))

    # ── motion (physically moves the stage) ─────────────────────────────────
    def move_to_um(self, which: str, target_um: float) -> None:
        if self._dev is None:
            raise StageControllerError("not connected")
        ax = self._axis(which)
        counts = ax.clamp_counts(ax.um_to_counts(target_um))
        self._dev.move_to_readout(ax.index, counts)

    def jog_um(self, which: str, delta_um: float) -> None:
        if self._dev is None:
            raise StageControllerError("not connected")
        ax = self._axis(which)
        cur = self._dev.get_status(ax.index).position
        target = ax.clamp_counts(int(round(cur + ax.sign * delta_um * ax.counts_per_um)))
        self._dev.move_to_readout(ax.index, target)

    def stop(self, which: str) -> None:
        if self._dev is not None:
            self._dev.stop(self._axis(which).index)

    def stop_all(self) -> None:
        if self._dev is not None:
            self._dev.stop_all([self._s.x.index, self._s.y.index])

    # ── frame / origin calibration ──────────────────────────────────────────
    # A HARD LIMIT hit re-references the controller's command origin, which
    # invalidates slope/offset — absolute go-to lands wrong, jog still works.
    #   set_center_here()  — no motion; declares "here" as 0,0.
    #   establish_frame()  — MOTION: drives to the reverse limit and re-measures.

    def read_xy_counts(self) -> tuple[int, int]:
        """Raw encoder counts (no origin applied) — the calibration works here."""
        if self._dev is None:
            raise StageControllerError("not connected")
        return (self._dev.get_status(self._s.x.index).position,
                self._dev.get_status(self._s.y.index).position)

    def set_center_here(self) -> dict[int, dict]:
        """Define the CURRENT position as 0,0 and put soft limits at ±½ inch
        around it (keeps the stage inside the encoder's no-wrap zone). Does NOT
        move the stage — centre it first. Persists to the shared config."""
        cx, cy = self.read_xy_counts()
        updates = {self._s.x.index: self._s.x.center_updates(cx, self._s.margin_um),
                   self._s.y.index: self._s.y.center_updates(cy, self._s.margin_um)}
        for ax, upd in ((self._s.x, updates[self._s.x.index]),
                        (self._s.y, updates[self._s.y.index])):
            ax.apply_updates(upd)
        save_axis_updates(updates)
        return updates

    def establish_frame(self, progress=None) -> dict[int, dict]:
        """MOTION — drives X then Y into their REVERSE hard limits, then probes
        twice to re-measure `enc = slope·cmd + offset`, restoring absolute
        positioning after a limit hit.

        Never invents an origin. The driver's geometric centre (reverse limit +
        half travel) is NOT where anyone wants 0,0 on this hardware, and
        overwriting a user-set origin with it silently moves the coordinate
        system. 0,0 comes from `set_center_here()` only.

        An existing origin is KEPT if it still falls inside the freshly measured
        travel and DROPPED if not — a limit hit can re-reference the encoder
        readout too, leaving `true_center` pointing at nothing. Dropping forces a
        deliberate re-zero instead of quietly reporting wrong microns.

        Blocks for a minute or more per axis — call it off the GUI thread.
        """
        if self._dev is None:
            raise StageControllerError("not connected")

        def say(msg: str) -> None:
            if progress is not None:
                progress(msg)

        updates: dict[int, dict] = {}
        dropped: list[str] = []
        for ax in (self._s.x, self._s.y):
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
        """MOTION: absolute move both axes back to the session home."""
        if self._dev is None:
            raise StageControllerError("not connected")
        if self._s.x.home_counts is None or self._s.y.home_counts is None:
            raise StageControllerError("no home set this session")
        for ax in (self._s.x, self._s.y):
            self._dev.move_to_readout(ax.index, ax.clamp_counts(int(ax.home_counts)))


class MockStageController:
    """Simulated stage: position eases toward the last commanded target."""
    _STEP_UM = 300.0        # max µm moved per read (visible motion at poll rate)

    def __init__(self, settings: StageSettings):
        self._s = settings
        self._pos = {"x": 0.0, "y": 0.0}
        self._target = {"x": 0.0, "y": 0.0}
        self._open = False

    def connect(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _axis(self, which: str) -> StageAxis:
        return self._s.x if which == "x" else self._s.y

    def _clamp_um(self, which: str, um: float) -> float:
        lo, hi = self._axis(which).soft_limits_um()
        return max(lo, min(hi, um))

    def read_xy_um(self) -> tuple[float, float]:
        # advance current toward target by up to _STEP_UM per read
        for k in ("x", "y"):
            d = self._target[k] - self._pos[k]
            if abs(d) <= self._STEP_UM:
                self._pos[k] = self._target[k]
            else:
                self._pos[k] += self._STEP_UM * (1 if d > 0 else -1)
        return (self._pos["x"], self._pos["y"])

    def move_to_um(self, which: str, target_um: float) -> None:
        self._target[which] = self._clamp_um(which, target_um)

    def jog_um(self, which: str, delta_um: float) -> None:
        self._target[which] = self._clamp_um(which, self._pos[which] + delta_um)

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
        updates = {self._s.x.index: self._s.x.center_updates(cx, self._s.margin_um),
                   self._s.y.index: self._s.y.center_updates(cy, self._s.margin_um)}
        for ax, upd in ((self._s.x, updates[self._s.x.index]),
                        (self._s.y, updates[self._s.y.index])):
            ax.apply_updates(upd)
        self._pos = {"x": 0.0, "y": 0.0}        # "here" is now the origin
        self._target = dict(self._pos)
        return updates

    def establish_frame(self, progress=None) -> dict[int, dict]:
        """Slope/offset only — same contract as the real one: the origin and the
        soft limits are left alone."""
        import time
        updates: dict[int, dict] = {}
        for ax in (self._s.x, self._s.y):
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
            self._target[k] = self._clamp_um(k, ax.to_um(ax.home_counts))
