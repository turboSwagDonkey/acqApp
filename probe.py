"""
Device connection probes.

Lightweight presence checks for each subsystem — enumeration only, so they
never open, hold, or reconfigure a device. That makes them safe to run at any
time, including while a session is acquiring (they won't fight the worker for
the camera / DAQ / serial port).

Each probe returns a ProbeResult(status, detail):
    "ok"      device detected
    "missing" driver present but no device found
    "error"   couldn't check (driver/import missing, or the check raised)
    "stub"    no hardware path exists yet (DMD)

Qt-free on purpose, so it can be unit-tested or run from a plain script.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_NI_DEVICE = "Dev3"
DEFAULT_STAGE_PORT = "COM54"


@dataclass
class ProbeResult:
    status: str          # ok | missing | error | stub
    detail: str


def _voltage_cam() -> ProbeResult:
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
            # Model name comes from the enumeration info — no Open() needed.
            names = ", ".join(d.GetModelName() for d in devices[:2])
            return ProbeResult("ok", names)
        return ProbeResult("missing", "no Basler camera (check USB 3.0 port)")
    except Exception as e:
        return ProbeResult("error", f"pypylon unavailable ({e})")


def _ni_device(name: str = DEFAULT_NI_DEVICE) -> ProbeResult:
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
    """The ALP *API*, not the device.

    Opening the ALP is the only way to know a DMD is really there, and opening
    it takes the USB from whoever holds it — a running session, or the
    standalone dmdGUI_project app. This monitor promises never to disturb a
    device, so it reports what can be checked without connecting. The DMD tab
    is where "is a real one attached" is answered, because that is the code
    that actually opened it.
    """
    try:
        import ALP4  # noqa: F401     (import only — the DLL loads on construction)
    except Exception as e:
        return ProbeResult("error", f"ALP4lib unavailable ({e})")
    try:
        from acqApp.devices.dmd import alp
        lib_dir, source = alp.resolve_lib_dir()
    except Exception as e:                       # pragma: no cover — import guard
        return ProbeResult("error", f"ALP API lookup failed ({e})")
    return ProbeResult("ok", f"ALP4 API via {source} "
                             f"({lib_dir or 'registry'}); not opened — "
                             f"one process at a time")


def _closed_loop() -> ProbeResult:
    """No device of its own: it watches one module and fires another."""
    return ProbeResult("stub", "software rule — no device of its own")


def probe(module: str, *, ni_device: str = DEFAULT_NI_DEVICE,
          stage_port: str = DEFAULT_STAGE_PORT) -> ProbeResult:
    """Probe one module by key. Never raises."""
    try:
        if module == "voltage_cam":
            return _voltage_cam()
        if module == "pupil_cam":
            return _pupil_cam()
        if module in ("wheel", "puffer"):
            return _ni_device(ni_device)
        if module == "stage":
            return _stage(stage_port)
        if module == "dmd":
            return _dmd()
        if module == "closed_loop":
            return _closed_loop()
        return ProbeResult("error", "unknown module")
    except Exception as e:                       # belt-and-braces
        return ProbeResult("error", str(e))


def probe_all(modules, *, ni_device: str = DEFAULT_NI_DEVICE,
              stage_port: str = DEFAULT_STAGE_PORT) -> dict[str, ProbeResult]:
    return {m: probe(m, ni_device=ni_device, stage_port=stage_port) for m in modules}
