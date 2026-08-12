"""
The interfaces the module adapters program against (§5b A1).

Every instrument here comes as a **pair** — a real driver and a mock twin — and
the `modules/` adapters are written against whichever one Emulate built. Until
this file existed, that contract was nine `getattr`/`hasattr` probes standing in
for a type, and the cost was not tidiness:

    "dmd_device": getattr(c, "device_name", "none")

writes a *plausible wrong value* into a session file the moment a mock drifts
from its real twin — a recording that really projected, filed as one that
didn't. That is the exact failure class the 2026-08-10 audit spent twenty items
removing, and it came within a forgotten property of happening on 2026-08-12,
when `device_name` / `resolution` / `on_pixels` were added to `DmdController`
and had to be remembered by hand on `MockDmdController`.

These are `typing.Protocol`s, so they are **structural**: no existing class
changes, nothing inherits from them, and nothing happens at import. What they
buy is a written-down contract with a name, which `tests/test_device_contracts.py`
then checks both twins against — because this project ships no type checker (the
rig installs `requirements.txt` and nothing else), so a Protocol that is never
asserted would catch exactly nothing.

They are deliberately **small and split** rather than one fat interface. The
eye-tracking LED has no `set_sink` and no `apply_settings`, and saying so by
leaving it out of `RecordingOutput` is more honest than giving it stub methods
so it can satisfy a bigger one. That is why `detach_sink` asks
`isinstance(c, RecordingOutput)` rather than `hasattr(c, "set_sink")`: same
answer, but the question now has a name and a reason.
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

Sink = Callable[[Any], None]


# ── acquisition ───────────────────────────────────────────────────────────────

@runtime_checkable
class DeviceWorker(Protocol):
    """A per-session acquisition thread. Every `ModuleAdapter.worker` is one.

    `get_latest()` hands the newest sample out **once** (the display tick is its
    consumer); `set_sink()` attaches the recording sink, which is fed every
    sample from the worker's own thread.
    """

    def get_latest(self) -> Any: ...
    def set_sink(self, sink: Sink | None) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class TimestampedWorker(DeviceWorker, Protocol):
    """A worker that admits which timebase its samples actually carry.

    `"hardware"` = spaced by a device clock; `"software"` = spaced by a Python
    loop, so the interval carries the scheduler's jitter. It goes straight into
    the session file, which is why a *default* here is dangerous: a mock that
    silently read `"unknown"` — or worse, inherited a real worker's
    `"hardware"` — would make a synthetic run look measured.
    """

    timestamp_source: str


@runtime_checkable
class CameraWorker(TimestampedWorker, Protocol):
    """A camera worker that counts the frames the DEVICE threw away.

    Those frames are gone from the file — the gap shows only as a jump in
    `voltage_cam_index` — so the count is the difference between a short
    recording and a lossy one.
    """

    @property
    def skipped_frames(self) -> int: ...


@runtime_checkable
class ClockedWorker(TimestampedWorker, Protocol):
    """A worker that reports the sample rate the device actually settled on.

    Asking for 120 Hz and getting 119.998 is normal — the board divides a
    100 MHz timebase and can only hit certain rates — but the file has to record
    what was used, not what was requested.
    """

    @property
    def actual_rate(self) -> float: ...


@runtime_checkable
class ExposureControl(Protocol):
    """A camera worker whose exposure can be changed while it runs."""

    def set_exposure(self, us: float) -> None: ...


# ── outputs ───────────────────────────────────────────────────────────────────

@runtime_checkable
class OutputController(Protocol):
    """An always-on output device, rebuilt whenever Emulate is toggled.

    Configured from its own settings panel, so `apply_settings` is what makes
    the panel more than decorative — audit #3 was this method not existing.
    """

    def apply_settings(self, settings: Any) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class RecordingOutput(OutputController, Protocol):
    """An output whose events belong in the session file (puffer, DMD).

    Not everything with a `close()` is one: the eye-tracking LED is an output
    and is deliberately **not** a `RecordingOutput`, because its on/off state is
    runtime illumination rather than an experimental event.
    """

    def set_sink(self, sink: Sink | None) -> None: ...


@runtime_checkable
class ProjectorController(RecordingOutput, Protocol):
    """An output that can describe itself into the session file.

    All three of these are recorded, and each answers a question the others
    cannot: `device_name` tells a session that projected from one that ran
    against the mock; `resolution` fixes the panel the geometry is relative to;
    and `on_pixels` catches the invisible failure — every mirror off is a legal
    frame and a projector showing nothing, which is also what a bad scale or
    offset produces.
    """

    @property
    def device_name(self) -> str: ...

    @property
    def resolution(self) -> tuple[int, int]: ...

    @property
    def on_pixels(self) -> int: ...


# ── the host ──────────────────────────────────────────────────────────────────

@runtime_checkable
class ModuleHost(Protocol):
    """What an adapter is allowed to ask of the window (§5b A4).

    The protocols above point *down* — what a module reads its device through.
    This one points **up**: the surface `MainWindow` offers its adapters, and
    the entire reason `modules/` and `main.py` stay independently readable.
    An adapter never reaches into the window's widgets; it asks for one of
    these nine things.

    Until this existed the surface was a sentence in the package's docstring
    naming seven members — and it had already drifted, because a docstring
    cannot be wrong out loud. Adding `module_keys` and `signal_sources` for the
    closed loop widened the surface by two with nothing to notice. That is the
    mild version; the failure it grows into is an adapter reaching past the
    listed members into `win._save_panel` or `win._modules`, at which point the
    two files are one file and the next instrument is four edits again.

    So `tests/test_device_contracts.py` checks both halves: that `MainWindow`
    still provides all of this, and — the half a Protocol alone cannot do —
    that every `self.win.X` in `modules/` is a member declared here. Adding a
    service is one line in this file. Helping yourself to one is a failing test.

    Deliberately typed with `Any` at the Qt boundary (`add_dock`'s widget and
    area, the returned dock, `sync`, `cam_handle`) so this module stays
    importable without PyQt6 — it is a contract, and a contract that drags in a
    GUI toolkit to be read is a worse one.
    """

    # ── session-wide services ────────────────────────────────────────────────
    @property
    def sync(self) -> Any:
        """The shared trigger bus (`sync.SyncController`).

        Both the schedule and the closed loop fire through it, so a
        rule-driven puff and a scheduled one are the same event downstream.
        """

    @property
    def cam_handle(self) -> Any:
        """The DCAM handle opened once at startup, or None.

        Handed to the worker rather than re-opened: re-opening a
        just-closed DCAM device is a known native crash.
        """

    # ── UI ───────────────────────────────────────────────────────────────────
    def status(self, message: str) -> None: ...
    def add_dock(self, title: str, widget: Any, area: Any,
                 accent: str = "sync") -> Any: ...
    def register_pg_view(self, view: Any) -> None:
        """Track a pyqtgraph view so the theme toggle can recolour it."""

    def set_expected_rate(self, mbps: float) -> None:
        """Feed the acquisition data rate to the Save tab's capacity estimate."""

    # ── cross-module queries ─────────────────────────────────────────────────
    def on_worker_error(self, msg: str) -> None:
        """Surface a device thread's exception instead of letting it abort the
        process. Every adapter connects its worker's `error` signal here."""

    def module_keys(self) -> list[str]:
        """The module keys loaded this session, in display order."""
        ...

    def signal_sources(self) -> list[Any]:
        """Every loaded module's `SignalSource`s, pooled for the closed loop.

        `list[Any]` rather than `list[SignalSource]` on purpose: the descriptor
        lives in `closed_loop.py`, which imports this module, and a contract
        file should not import a feature back.
        """
        ...
