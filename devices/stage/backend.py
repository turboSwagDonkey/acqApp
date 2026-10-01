"""Stage backend registry: which driver talks to the controller plugged in,
and how to tell automatically. A new controller is a driver module with the
surface below, registered in BACKENDS; control.py doesn't change.

Every backend driver exposes the same axis-indexed surface:
    open() / close() / is_open
    get_status(axis) -> object with .position (int) and .moving (bool)
    move_to_readout(axis, target_counts)   # MOTION
    jog_by_readout(axis, delta_counts, current_counts=None)   # MOTION
    stop(axis) / stop_all(axes)            # MOTION
    set_linear_map(axis, slope, offset) / linear_map(axis)
`axis` is the driver's own address (MCM6101 0/1/2, MCM301 slots 4/5/6),
carried opaquely from StageAxis.index, so the two backends' configs index
the same logical X/Y/Z differently.

`establish_frame` is MCM6101-only: it works around that controller
re-referencing its origin on every hard-limit hit. The MCM301's readout is a
stable encoder count, and StageController.establish_frame() says so.
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
    """Only answering HW_REQ_INFO over the open port tells an MCM6101 apart
    from any other USB-CDC device."""
    from .driver import MCM6101
    dev = MCM6101(port)
    try:
        dev.open()
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
    """Connectionless (the DLL enumerates without opening a port). A lone
    MCM301 matches on any port: Windows renumbers COM ports, and this lists
    only MCM301s."""
    from .mcm301_driver import com_name, list_devices
    try:
        devices = list_devices()
    except Exception:
        return False
    if any(com_name(com) == port.upper() for _sn, com in devices):
        return True
    return len(devices) == 1


# Probed in order: the connectionless mcm301 probe before the port-opening
# mcm6101 one.
BACKENDS: dict[str, _Backend] = {
    "mcm301":  _Backend("mcm301",  _open_mcm301,  _probe_mcm301),
    "mcm6101": _Backend("mcm6101", _open_mcm6101, _probe_mcm6101),
}


def probe_port(port: str) -> str | None:
    """The backend whose hardware is on `port`, or None; leaves nothing
    open."""
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
    """Probe `port` and connect -> (backend_name, connected_driver)."""
    name = probe_port(port)
    if name is None:
        raise BackendError(
            f"No stage controller answered on {port}. Is the controller "
            f"powered on and its USB cable plugged in? (Tried: "
            f"{', '.join(sorted(BACKENDS))}.)"
        )
    return name, open_backend(name, port)
