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
        Save tab. Two numbers because they differ by 2x at full frame, and the
        disk fills at the smaller one."""

    def on_worker_error(self, msg: str) -> None:
        """Surface a device thread's exception instead of letting it abort the
        process."""

    def module_keys(self) -> list[str]:
        """Loaded module keys, in display order."""
        ...

    def signal_sources(self) -> list[Any]:
        """Every module's `SignalSource`s, pooled for the closed loop."""
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
