"""The interfaces the module adapters program against.

Structural Protocols: nothing inherits; tests/test_device_contracts.py
enforces them. (getattr probes they replaced once filed a real projection as
none.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

Sink = Callable[[Any], None]


@dataclass(frozen=True)
class SignalSource:
    """A live scalar one module offers another (the wheel's speed, for
    visuomotor). `read()` -> (value, acquired_at) or None while not running;
    must not consume. `acquired_at` is perf_counter at acquisition."""
    key:   str
    label: str
    units: str
    read:  Callable[[], tuple[float, float] | None]


# ── acquisition ───────────────────────────────────────────────────────────────

@runtime_checkable
class DeviceWorker(Protocol):
    """A per-session acquisition thread; `get_latest()` hands each sample out once."""

    def get_latest(self) -> Any: ...
    def set_sink(self, sink: Sink | None) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@runtime_checkable
class TimestampedWorker(DeviceWorker, Protocol):
    """"hardware" (device clock) or "software" (Python loop, with jitter).
    Filed, so it has no default."""

    timestamp_source: str


@runtime_checkable
class CameraWorker(TimestampedWorker, Protocol):
    @property
    def skipped_frames(self) -> int: ...


@runtime_checkable
class ClockedWorker(TimestampedWorker, Protocol):
    """The rate the device settled on (119.998 for 120), which is what's filed."""

    @property
    def actual_rate(self) -> float: ...


@runtime_checkable
class ExposureControl(Protocol):
    def set_exposure(self, us: float) -> None: ...


# ── outputs ───────────────────────────────────────────────────────────────────

@runtime_checkable
class OutputController(Protocol):
    """An always-on output, rebuilt when Emulate is toggled.

    On an open failure: if `apply_settings` can reopen in place (the puffer),
    stay inert; if not (the LED), raise from `__init__` so the adapter swaps
    in the mock — an inert object would be indistinguishable from a live one.
    """

    def apply_settings(self, settings: Any) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class RecordingOutput(OutputController, Protocol):
    """An output whose events belong in the file."""

    def set_sink(self, sink: Sink | None) -> None: ...


@runtime_checkable
class RawProjector(Protocol):
    """Displays a frame at device size, untransformed — the calibration sweep
    needs a guarantee nothing reshapes the geometry it measures."""

    @property
    def resolution(self) -> tuple[int, int]: ...

    def project_frame(self, frame: Any) -> None: ...

    def stop(self) -> None: ...


@runtime_checkable
class ProjectorController(RecordingOutput, Protocol):
    """`on_pixels` catches an all-off frame: legal, but shows nothing."""

    @property
    def device_name(self) -> str: ...

    @property
    def resolution(self) -> tuple[int, int]: ...

    @property
    def on_pixels(self) -> int: ...


# ── what a routine may drive ──────────────────────────────────────────────────
# Declared by the adapter, so a routine drives the module as loaded (soft
# limits, mock or real).

@runtime_checkable
class StageTarget(Protocol):
    """`move_to` returns at once; `is_moving` is how a routine waits."""

    def move_to(self, x_um: float | None, y_um: float | None,
               z_um: float | None = None) -> None:
        """None leaves that axis where it is."""

    def is_moving(self) -> bool: ...

    def stop_motion(self) -> None:
        """Called on any fault, so it must not raise blindly."""

    def limits_um(self) -> tuple[tuple[float, float] | None,
                                 tuple[float, float] | None]: ...

    def has_z(self) -> bool: ...

    def z_limits_um(self) -> tuple[float, float] | None: ...


@runtime_checkable
class PatternTarget(Protocol):
    def set_pattern(self, path: str) -> None: ...

    def set_light(self, on: bool) -> None:
        """The one call that emits light."""


@runtime_checkable
class LedTarget(Protocol):
    def set_led(self, on: bool) -> None: ...


@runtime_checkable
class PufferTarget(Protocol):
    def fire(self, duration_s: float | None = None) -> None:
        """None uses the configured duration."""


# ── the host ──────────────────────────────────────────────────────────────────

@runtime_checkable
class ModuleHost(Protocol):
    """What an adapter may ask of the window. The contract test also scans
    adapter source for reaches past it (`win._save_panel`). Methods taking a
    module `key` return None when it isn't loaded or has no such notion."""

    @property
    def sync(self) -> Any: ...

    @property
    def cam_handle(self) -> Any:
        """The DCAM handle opened once at startup; re-opening crashes natively."""

    def dcimg_enabled(self) -> bool: ...

    def dcimg_target(self, stream: str) -> Any:
        """The .dcimg path, or None for the normal sink."""

    def camera_ready(self, stream: str) -> bool:
        """False while a .dcimg roll has the camera stopped; True with no
        such notion."""

    def dcimg_frames(self, stream: str) -> Any:
        """Frames the .dcimg holds, or None if none is open. Stands in for
        `Recorder.offered()`, which DCAM's own writes never reach."""

    def status(self, message: str) -> None: ...
    def add_dock(self, title: str, widget: Any, area: Any,
                 accent: str = "sync") -> Any: ...

    def register_pg_view(self, view: Any) -> None:
        """So the theme toggle can recolour it."""

    def set_expected_rate(self, mbps: float, writer_mbps: float = 0.0) -> None:
        """Two numbers: the disk fills at the smaller one."""

    def on_worker_error(self, msg: str) -> None: ...

    def set_modules(self, keys) -> tuple[list[str], list[str]]:
        """-> (loaded, unloaded). Raises while recording."""

    def module_keys(self) -> list[str]: ...

    def signal_sources(self) -> list[SignalSource]: ...

    def set_live(self, on: bool) -> bool:
        """Returns the previous state, so a caller can put it back."""

    def set_recording(self, on: bool) -> bool:
        """Returns the previous state, so a caller stops only what it started."""

    def is_recording(self) -> bool: ...

    def camera_preset(self, key: str) -> str | None: ...

    def set_camera_preset(self, key: str, preset: str) -> str | None:
        """Returns the previous preset. Applies at the next session start."""

    def camera_binning(self, key: str) -> int | None: ...

    def set_camera_binning(self, key: str, n: int) -> int | None:
        """Returns the previous value. Applies at the next session start."""

    def set_camera_trigger(self, key: str, on: bool) -> bool | None:
        """External edge (True) or Internal, restarting live view if needed.
        False: refused because a recording is running."""

    def routine_arming_trigger(self, on: bool) -> None:
        """Brackets a routine's arm + session open, so `_start_session()`
        doesn't reset the camera to Internal under it."""

    def set_camera_burst(self, key: str, n: int) -> bool | None:
        """Frames per edge (0 = until re-armed), restarting live view if
        needed. False: refused because a recording is running."""

    def camera_burst_frames(self, key: str) -> int | None:
        """Real frames of the burst the last gate caught; None outside burst."""

    def rearm_camera_trigger(self, key: str) -> bool | None:
        """Queue a re-gate so the next edge is detectable. False: no worker."""

    def arm_camera_with_next_file(self, key: str) -> bool | None:
        """Make the next .dcimg swap re-arm too. False: no .dcimg open."""

    def seal_camera_file(self, key: str) -> bool | None:
        """Close the open .dcimg early; the recording stays open until the
        next roll. False: no .dcimg open."""

    def camera_trigger_gate(self, key: str) -> tuple[int, int] | None:
        """(re-arms completed, frames since the last)."""

    def stage_target(self) -> Any: ...

    def pattern_target(self) -> Any: ...

    def led_target(self) -> Any: ...

    def puffer_target(self) -> Any: ...

    def frame_rate_hz(self) -> float | None:
        """For estimates only."""

    def latest_frame(self, key: str) -> Any:
        """The cached newest frame; never commands the camera."""

    def latest_frame_preset(self, key: str) -> str | None:
        """The preset that frame was captured under (`camera_preset` can
        name one not yet in effect)."""

    def active_fov_name(self) -> str: ...

    def roll_recording(self) -> bool:
        """Close and reopen with no gap the routine could mistake for a stop.
        Returns whether the new one started."""

    def set_routine_save_context(self, fov: str | None, trial: int | None,
                                 coords: tuple[float | None, float | None,
                                              float | None] | None = None
                                 ) -> None:
        """Name the next file by (FOV, trial); None, None clears. `coords`
        for a "custom" FOV go to a sidecar."""

    def recording_path(self) -> Any:
        """The file/folder being recorded to, or None."""

    def routine_folder(self) -> Any:
        """Today's folder for routine trials (where the edge log goes)."""
