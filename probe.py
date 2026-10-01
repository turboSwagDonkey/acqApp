"""
Device connection probes: enumeration only, never open, so safe mid-session
with a worker holding the device. Qt-free, so it runs from a plain script.

ProbeResult.status:
    "ok"      device detected
    "missing" driver present but no device found
    "error"   couldn't check (driver/import missing, or the check raised)
    "stub"    module has no device of its own
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_STAGE_PORT = "COM54"


@dataclass
class ProbeResult:
    status: str          # ok | missing | error | stub
    detail: str


def _voltage_cam(cam_open: bool = False) -> ProbeResult:
    """`cam_open`: the app holds the camera, so skip enumeration —
    `DCAM.get_cameras_number()` costs ~6.5 s on every call, on the GUI
    thread."""
    if cam_open:
        return ProbeResult("ok", "open and held by this app")
    try:
        from pylablib.devices import DCAM
        n = DCAM.get_cameras_number()
        if n > 0:
            return ProbeResult("ok", f"{n} DCAM camera(s) detected")
        return ProbeResult("missing", "no DCAM camera")
    except Exception as e:
        return ProbeResult("error", f"DCAM/pylablib unavailable ({e})")


def _pupil_cam() -> ProbeResult:
    try:
        from pypylon import pylon
        devices = pylon.TlFactory.GetInstance().EnumerateDevices()
        if devices:
            names = ", ".join(d.GetModelName() for d in devices[:2])
            return ProbeResult("ok", names)
        return ProbeResult("missing", "no Basler camera (check USB 3.0 port)")
    except Exception as e:
        return ProbeResult("error", f"pypylon unavailable ({e})")


def _ni_device(name: str | None = None) -> ProbeResult:
    # Per call, not a default arg: the test harness re-points config's file
    # after this module is imported.
    if name is None:
        from acqApp import config
        name = config.rig_device()
    try:
        import nidaqmx
        present = [d.name for d in nidaqmx.system.System.local().devices]
        if name in present:
            try:
                product = nidaqmx.system.Device(name).product_type
            except Exception:
                product = ""
            return ProbeResult("ok", f"{name} {product}".strip())
        have = ", ".join(present) or "none"
        return ProbeResult("missing", f"{name} not present (found: {have})")
    except Exception as e:
        return ProbeResult("error", f"NI-DAQmx unavailable ({e})")


def _stage(port: str = DEFAULT_STAGE_PORT) -> ProbeResult:
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
        if port in ports:
            return ProbeResult("ok", f"{port} present")
        have = ", ".join(ports) or "none"
        return ProbeResult("missing", f"{port} not found (found: {have})")
    except Exception as e:
        return ProbeResult("error", f"pyserial unavailable ({e})")


def _dmd() -> ProbeResult:
    """The ALP API only: opening the ALP is the only way to find a DMD, and it
    takes the USB from whoever holds it (a session, dmdGUI_project)."""
    try:
        import ALP4  # noqa: F401     (import only — the DLL loads on construction)
    except Exception as e:
        return ProbeResult("error", f"ALP4lib unavailable ({e})")
    try:
        from acqApp.devices.dmd import alp
        lib_dir, source = alp.resolve_lib_dir()
    except Exception as e:                       # pragma: no cover
        return ProbeResult("error", f"ALP API lookup failed ({e})")
    return ProbeResult("ok", f"ALP4 API via {source} "
                             f"({lib_dir or 'registry'}); not opened — "
                             f"one process at a time")


def _closed_loop() -> ProbeResult:
    return ProbeResult("stub", "software rule — no device of its own")


def _vis_stim() -> ProbeResult:
    return ProbeResult("stub", "shows on a display screen, gated by the "
                              "shared session clock — no device of its own")


def _mirror() -> ProbeResult:
    return ProbeResult("stub", "operator-asserted state — no device of its "
                              "own (ThorImage owns the real switch)")


def probe(module: str, *, ni_device: str | None = None,
          stage_port: str = DEFAULT_STAGE_PORT,
          cam_open: bool = False) -> ProbeResult:
    """Probe one module by key. Never raises."""
    try:
        if module == "voltage_cam":
            return _voltage_cam(cam_open)
        if module == "pupil_cam":
            return _pupil_cam()
        if module in ("wheel", "puffer"):
            return _ni_device(ni_device)
        if module == "stage":
            return _stage(stage_port)
        if module == "dmd":
            return _dmd()
        if module == "vis_stim":
            return _vis_stim()
        if module == "mirror":
            return _mirror()
        if module == "closed_loop":
            return _closed_loop()
        return ProbeResult("error", "unknown module")
    except Exception as e:                       # belt-and-braces
        return ProbeResult("error", str(e))


def probe_all(modules, *, ni_device: str | None = None,
              stage_port: str = DEFAULT_STAGE_PORT,
              cam_open: bool = False) -> dict[str, ProbeResult]:
    # wheel and puffer share one NI device: enumerate it once.
    ni_result: ProbeResult | None = None
    out: dict[str, ProbeResult] = {}
    for m in modules:
        if m in ("wheel", "puffer"):
            if ni_result is None:
                ni_result = _ni_device(ni_device)
            out[m] = ni_result
        else:
            out[m] = probe(m, ni_device=ni_device, stage_port=stage_port,
                           cam_open=cam_open)
    return out
