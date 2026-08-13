"""
The interfaces the module adapters program against (§5b A1, A4).

Every instrument comes as a **pair** — a real driver and a mock twin — and the
`modules/` adapters are written against whichever one Emulate built. That
contract used to be nine `getattr`/`hasattr` probes, and the cost was not
tidiness: `getattr(c, "device_name", "none")` files a session that really
projected as one that didn't, the moment a mock drifts from its real twin.

These are `Protocol`s, so they are structural: nothing inherits from them and
nothing happens at import. This project ships no type checker, so a Protocol
nobody asserts catches nothing — `tests/test_device_contracts.py` is what makes
them bite.

Deliberately small and split rather than one fat interface: the eye-tracking
LED has no `set_sink`, and leaving it out of `RecordingOutput` is more honest
than stub methods. That is why `detach_sink` asks
`isinstance(c, RecordingOutput)` rather than `hasattr(c, "set_sink")`.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

Sink = Callable[[Any], None]


# ── acquisition ───────────────────────────────────────────────────────────────

@runtime_checkable
class DeviceWorker(Protocol):
    """A per-session acquisition thread. Every `ModuleAdapter.worker` is one.

    `get_latest()` hands the newest sample out **once** (the display tick is its
    consumer); `set_sink()` takes the recording sink, fed from the worker's own
    thread.
    """

    def get_latest(self) -> Any: ...
    def set_sink(self, sink: Sink | None) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class TimestampedWorker(DeviceWorker, Protocol):
    """Which timebase the samples actually carry: `"hardware"` = a device clock,
    `"software"` = a Python loop, so the interval carries scheduler jitter.

    It goes straight into the session file, which is why a *default* here is
    dangerous — a mock reading `"unknown"`, or inheriting a real worker's
    `"hardware"`, makes a synthetic run look measured.
    """

    timestamp_source: str


@runtime_checkable
class CameraWorker(TimestampedWorker, Protocol):
    """Frames the DEVICE threw away. They are gone from the file and show only
    as a jump in `voltage_cam_index`, so the count is the difference between a
    short recording and a lossy one."""

    @property
    def skipped_frames(self) -> int: ...


@runtime_checkable
class ClockedWorker(TimestampedWorker, Protocol):
    """The rate the device actually settled on. Asking for 120 Hz and getting
    119.998 is normal — the board divides a 100 MHz timebase — but the file has
    to record what was used, not what was requested."""

    @property
    def actual_rate(self) -> float: ...


@runtime_checkable
class ExposureControl(Protocol):
    """A camera worker whose exposure can change while it runs."""

    def set_exposure(self, us: float) -> None: ...


# ── outputs ───────────────────────────────────────────────────────────────────

@runtime_checkable
class OutputController(Protocol):
    """An always-on output, rebuilt whenever Emulate is toggled. Configured from
    its own panel, so `apply_settings` is what makes that panel more than
    decorative — audit #3 was this method not existing."""

    def apply_settings(self, settings: Any) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class RecordingOutput(OutputController, Protocol):
    """An output whose events belong in the session file (puffer, DMD). Not
    everything with a `close()` is one — see the LED, above."""

    def set_sink(self, sink: Sink | None) -> None: ...


@runtime_checkable
class ProjectorController(RecordingOutput, Protocol):
    """An output that can describe itself into the session file. Each of these
    answers something the others cannot: `device_name` separates a session that
    projected from one that ran against the mock, `resolution` fixes the panel
    the geometry is relative to, and `on_pixels` catches the invisible failure —
    every mirror off is a legal frame and a projector showing nothing, which is
    also what a bad scale or offset produces."""

    @property
    def device_name(self) -> str: ...

    @property
    def resolution(self) -> tuple[int, int]: ...

    @property
    def on_pixels(self) -> int: ...


# ── the host ──────────────────────────────────────────────────────────────────

@runtime_checkable
class ModuleHost(Protocol):
    """What an adapter may ask of the window — the other direction to the
    protocols above, and the reason `modules/` and `main.py` stay independently
    readable.

    The failure this prevents is an adapter reaching past these into
    `win._save_panel`, at which point the two are one file again. A Protocol
    alone cannot see that, so the test also scans the adapters' source: adding a
    service is a line here, helping yourself to one is a failing test.

    `Any` at the Qt boundary keeps this module importable without PyQt6.
    """

    @property
    def sync(self) -> Any:
        """The shared trigger bus. Schedule and closed loop both fire through
        it, so a rule-driven puff and a scheduled one are one event."""

    @property
    def cam_handle(self) -> Any:
        """The DCAM handle opened once at startup, or None. Handed to the worker
        rather than re-opened: re-opening a just-closed DCAM device crashes the
        driver natively."""

    def status(self, message: str) -> None: ...
    def add_dock(self, title: str, widget: Any, area: Any,
                 accent: str = "sync") -> Any: ...
    def register_pg_view(self, view: Any) -> None:
        """Track a pyqtgraph view so the theme toggle can recolour it."""

    def set_expected_rate(self, mbps: float) -> None:
        """Feed the data rate to the Save tab's capacity estimate."""

    def on_worker_error(self, msg: str) -> None:
        """Surface a device thread's exception instead of letting it abort the
        process. Every adapter connects its worker's `error` signal here."""

    def module_keys(self) -> list[str]:
        """Loaded module keys, in display order."""
        ...

    def signal_sources(self) -> list[Any]:
        """Every module's `SignalSource`s, pooled for the closed loop. `Any`
        because the descriptor lives in `closed_loop.py`, which imports this."""
        ...
