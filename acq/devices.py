"""The interfaces the module adapters program against (§5b A1, A4).

Every instrument is a real/mock pair and the adapters are written against
whichever Emulate built. These replace nine `getattr`/`hasattr` probes, whose
cost was not tidiness: `getattr(c, "device_name", "none")` files a session that
really projected as one that didn't.

Structural `Protocol`s — nothing inherits, nothing happens at import, and this
project ships no type checker, so `tests/test_device_contracts.py` is what makes
them bite. Split rather than fat: the eye-tracking LED has no `set_sink`, so it
is not a `RecordingOutput`.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

Sink = Callable[[Any], None]


# ── acquisition ───────────────────────────────────────────────────────────────

@runtime_checkable
class DeviceWorker(Protocol):
    """A per-session acquisition thread. `get_latest()` hands the newest sample
    out once — the display tick is its consumer."""

    def get_latest(self) -> Any: ...
    def set_sink(self, sink: Sink | None) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class TimestampedWorker(DeviceWorker, Protocol):
    """`"hardware"` = a device clock, `"software"` = a Python loop carrying
    scheduler jitter. Goes into the file, so a default here would make a
    synthetic run look measured."""

    timestamp_source: str


@runtime_checkable
class CameraWorker(TimestampedWorker, Protocol):
    """Frames the device threw away — gone from the file, visible only as a jump
    in `voltage_cam_index`."""

    @property
    def skipped_frames(self) -> int: ...


@runtime_checkable
class ClockedWorker(TimestampedWorker, Protocol):
    """The rate the device settled on. 119.998 for a requested 120 is normal;
    the file records what was used, not what was asked for."""

    @property
    def actual_rate(self) -> float: ...


@runtime_checkable
class ExposureControl(Protocol):
    """A camera worker whose exposure can change while it runs."""

    def set_exposure(self, us: float) -> None: ...


# ── outputs ───────────────────────────────────────────────────────────────────

@runtime_checkable
class OutputController(Protocol):
    """An always-on output, rebuilt when Emulate is toggled. `apply_settings` is
    what makes its panel more than decorative (audit #3)."""

    def apply_settings(self, settings: Any) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class RecordingOutput(OutputController, Protocol):
    """An output whose events belong in the file. Not everything with `close()`
    is one — see the LED."""

    def set_sink(self, sink: Sink | None) -> None: ...


@runtime_checkable
class RawProjector(Protocol):
    """Displays a frame at the device's own size, untransformed.

    Split from `ProjectorController` rather than folded into it because the one
    client is the calibration sweep, and what that client needs is precisely
    the guarantee that nothing reshapes the frame — `build_frame`'s
    scale/rotation/offset (and `fit`, which overrides all three) would transform
    the geometry being measured.
    """

    @property
    def resolution(self) -> tuple[int, int]: ...

    def project_frame(self, frame: Any) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class ProjectorController(RecordingOutput, Protocol):
    """Describes itself into the file. `device_name` separates a real projection
    from the mock, `resolution` fixes the panel the geometry is relative to, and
    `on_pixels` catches every-mirror-off — a legal frame showing nothing."""

    @property
    def device_name(self) -> str: ...

    @property
    def resolution(self) -> tuple[int, int]: ...

    @property
    def on_pixels(self) -> int: ...


# ── what a routine may drive ──────────────────────────────────────────────────
# Declared by the ADAPTER, not the driver: an experiment routine drives the
# module as it is loaded — its soft limits, mock or real — and adding one of
# these to an instrument is the whole cost of making it routine-drivable, the
# way declaring a SignalSource is the whole cost of making a quantity
# triggerable. Split in two because the stage is not a projector.

@runtime_checkable
class StageTarget(Protocol):
    """A module a routine can send to an XY position.

    No "is it moving?": the MCM6101 answers only over the serial link the
    position poller already shares, and a routine ticking at 20 Hz would flood
    it. Arrival is the operator's `settle_s` (PLAN §6 (3)) until a cheap arrival
    signal exists — `RoutineHooks.moving` is the seam it plugs into.
    """

    def move_to(self, x_um: float | None, y_um: float | None) -> None:
        """Move; None leaves that axis where it is."""

    def stop_motion(self) -> None:
        """Stop both axes. Called on any fault, so it must not raise blindly."""

    def limits_um(self) -> tuple[tuple[float, float] | None,
                                 tuple[float, float] | None]:
        """(x, y) soft limits, so a routine is validated before it starts."""


@runtime_checkable
class PatternTarget(Protocol):
    """A module a routine can put a pattern up on and take it down again."""

    def set_pattern(self, path: str) -> None: ...

    def set_light(self, on: bool) -> None:
        """The one call that emits light. Everything else here is reversible."""


# ── the host ──────────────────────────────────────────────────────────────────

@runtime_checkable
class ModuleHost(Protocol):
    """What an adapter may ask of the window — reaching past it into
    `win._save_panel` makes `adapters/` and `main.py` one file again.

    A Protocol cannot see that, so the test also scans the adapters' source:
    adding a service is a line here. `Any` at the Qt boundary keeps this
    importable without PyQt6.
    """

    @property
    def sync(self) -> Any:
        """Shared trigger bus — a rule-driven puff and a scheduled one are one
        event."""

    @property
    def cam_handle(self) -> Any:
        """The DCAM handle opened once at startup, or None. Re-opening a
        just-closed DCAM device crashes the driver natively."""

    def status(self, message: str) -> None: ...
    def add_dock(self, title: str, widget: Any, area: Any,
                 accent: str = "sync") -> Any: ...

    def register_pg_view(self, view: Any) -> None:
        """Track a pyqtgraph view so the theme toggle can recolour it."""

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        """Feed the acquisition rate, and what the write path sustains, to the
        Save tab. Two numbers because the disk fills at the smaller one."""

    def on_worker_error(self, msg: str) -> None:
        """Surface a device thread's exception instead of letting it abort the
        process."""

    def set_modules(self, keys) -> tuple[list[str], list[str]]:
        """Load/unload instruments in place → (loaded, unloaded).

        Raises RuntimeError while recording: the file's `modules` attribute is
        written at record start, and a stream that appears or vanishes mid-file
        is not describable by it.
        """
        ...

    def module_keys(self) -> list[str]:
        """Loaded module keys, in display order."""
        ...

    def signal_sources(self) -> list[Any]:
        """Every module's `SignalSource`s, pooled for the closed loop."""
        ...

    def set_live(self, on: bool) -> bool:
        """Turn the window's live view on or off; returns its PREVIOUS state.

        Added deliberately (§5b A4): the DMD calibration images each pattern
        with the voltage camera, so it needs frames flowing — and requiring the
        operator to press Live view first, in another part of the window, before
        a dialog that then complains, is a worse design than letting the dialog
        do it and put it back. The return value is what makes putting it back
        possible.
        """
        ...

    def set_recording(self, on: bool) -> bool:
        """Start/stop recording; returns its PREVIOUS state.

        The twin of `set_live`, and the same argument: an experiment routine
        cannot run a step without a file open, and the panel that starts the
        routine is the right place to open one. The return value is what lets a
        caller stop only a recording it started itself.
        """
        ...

    def stage_target(self) -> Any:
        """The loaded module a routine may move, or None.

        Pooled by the window like `signal_sources()`, and for the same reason:
        the routine must not import the stage adapter, and the stage must not
        know routines exist.
        """
        ...

    def pattern_target(self) -> Any:
        """The loaded module a routine may project through, or None."""
        ...

    def frame_rate_hz(self) -> float | None:
        """The loaded camera's configured frame rate, or None.

        Pooled like the two above, and for the same reason: the routine panel
        estimates how long "100 frames" takes without importing a camera. It is
        an ESTIMATE — frames and seconds are still never interconverted where a
        step is recorded (`routines/settings.py`).
        """
        ...

    def latest_frame(self, key: str) -> Any:
        """The newest frame from another module's camera, or None.

        Added deliberately (§5b A4 is the rule this is the case for): the DMD's
        ROI editor draws on an **ORCA** frame, because the voltage camera is the
        imaging path the DMD projects into — so a panel in the DMD tab needs a
        frame owned by `voltage_cam`. Reading it through the host keeps the two
        modules from importing each other.

        Only the newest frame, never a grab: this must not command a camera,
        because the operator decides when the DMD is all-on.
        """
        ...
