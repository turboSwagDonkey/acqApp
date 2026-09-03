"""
Backend registry for the stage: which driver module talks to whatever
controller is actually plugged in, and how to figure that out automatically.

The hardware has already been swapped once (MCM6101 -> MCM301, 2026-09) and
will likely be swapped again. That should mean plugging in the new
controller and letting the app notice -- not editing control.py. Adding a
new controller is: write its driver module (open/close/get_status/
move_to_readout/stop, matching the shape of driver.py or mcm301_driver.py),
then register it below.

Every backend driver exposes the same axis-indexed surface:
    open() / close() / is_open
    get_status(axis) -> object with .position (int) and .moving (bool)
    move_to_readout(axis, target_counts)   # MOTION
    jog_by_readout(axis, delta_counts, current_counts=None)   # MOTION
    stop(axis) / stop_all(axes)            # MOTION
    set_linear_map(axis, slope, offset) / linear_map(axis)
`axis` is whatever address that backend's own driver expects (0/1/2 for the
MCM6101's dest-offset scheme, 4/5/6 for the MCM301's fixed slots) -- it is
carried through opaquely from StageAxis.index in the calibration config, so
the two backends' configs simply use different index values for the same
logical X/Y/Z.

`establish_frame` (the MCM6101's command-origin recalibration) is NOT part
of the common surface: it exists to work around that controller's specific
quirk of re-referencing its origin on every hard-limit hit. A backend
without that quirk (the MCM301: position is already a stable encoder count)
just doesn't implement it, and StageController.establish_frame() reports
that clearly instead of AttributeError-ing.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


class BackendError(Exception):
    pass


@dataclass(frozen=True)
class _Backend:
    name: str
    open: Callable[[str], object]    # (port) -> connected, ready-to-use driver
    probe: Callable[[str], bool]     # (port) -> True if this backend's hardware answers there


def _open_mcm6101(port: str):
    from .driver import MCM6101
    dev = MCM6101(port)
    dev.open()
    return dev


def _probe_mcm6101(port: str) -> bool:
    """The MCM6101 only identifies itself by actually answering APT's
    HW_REQ_INFO over the open serial port -- there is no OS-level way to
    tell it apart from any other USB-CDC device without talking to it."""
    from .driver import MCM6101
    dev = MCM6101(port)
    try:
        dev.open()
    except Exception:
        return False
    try:
        dev.get_info()
        return True
    except Exception:
        return False
    finally:
        dev.close()


def _open_mcm301(port: str):
    from .mcm301_driver import MCM301
    dev = MCM301(port)
    dev.open()
    return dev


def _probe_mcm301(port: str) -> bool:
    """The MCM301's vendor DLL enumerates its own devices without opening a
    port, so this probe is connectionless and cheap -- run it first.

    A single connected MCM301 counts as a match even when it isn't on the
    configured port: Windows renumbers COM ports whenever it feels like it,
    and this enumeration lists ONLY MCM301-family devices, so there is nothing
    else it could be confused with."""
    from .mcm301_driver import com_name, list_devices
    try:
        devices = list_devices()
    except Exception:
        return False
    if any(com_name(com) == port.upper() for _sn, com in devices):
        return True
    return len(devices) == 1


# Probed in this order. mcm301's probe is a safe, connectionless enumeration;
# mcm6101's has to open the serial port, so it runs only if nothing cheaper matched.
BACKENDS: dict[str, _Backend] = {
    "mcm301":  _Backend("mcm301",  _open_mcm301,  _probe_mcm301),
    "mcm6101": _Backend("mcm6101", _open_mcm6101, _probe_mcm6101),
}


def probe_port(port: str) -> str | None:
    """Identify which registered backend's hardware is on `port`, leaving
    nothing open. Returns the backend name, or None if nothing recognized it."""
    for name, backend in BACKENDS.items():
        try:
            if backend.probe(port):
                return name
        except Exception:
            continue
    return None


def open_backend(name: str, port: str):
    """Connect using a specific backend by name (already open on return)."""
    backend = BACKENDS.get(name)
    if backend is None:
        raise BackendError(f"Unknown stage controller backend {name!r}. "
                            f"Known: {sorted(BACKENDS)}")
    return backend.open(port)


def connect_auto(port: str) -> tuple[str, object]:
    """Probe `port` and connect with whichever backend recognizes it.
    Returns (backend_name, connected_driver)."""
    name = probe_port(port)
    if name is None:
        raise BackendError(
            f"No stage controller answered on {port}. Is the controller "
            f"powered on and its USB cable plugged in? (Tried: "
            f"{', '.join(sorted(BACKENDS))}.)"
        )
    return name, open_backend(name, port)
