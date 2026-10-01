"""Driver for a Thorlabs MCM301 3-channel stepper controller (the MCM6101's
successor; see driver.py). Its only documented interface is the vendor DLL
(MCM301Lib_x64.dll), which owns the serial framing, so this wraps it via
ctypes.

Verified on this hardware:
  * Connection : USB CDC, found by SERIAL NUMBER; the port is advisory. It
                 was COM3 until Windows gave that number to a second device
                 too, which wedged it (see open()); now COM10.
  * Addressing : FIXED slots 4, 5, 6 (not 0-indexed axes like the MCM6101).
  * Axes       : slots 4/5 are MMP-201121 (0.5 um/count, travel +-50800
                 counts); slot 6 is a PLS-283529, the Z/focus axis
                 (2026-09-13).

Only methods marked MOTION move anything.
"""
from __future__ import annotations
import ctypes
import re
import threading
from ctypes import c_int, c_byte, c_uint, c_char_p, create_string_buffer, byref
from dataclasses import dataclass
from pathlib import Path

# Fixed slots (Thorlabs MCM301 SDK docs).
SLOT_X = 4
SLOT_Y = 5
SLOT_Z = 6  # focus; driven via StageSettings.z when configured

DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT_S = 3

# status_bit flags (MCM301 SDK "GetMotStatus" docs)
STATUS_FWD_HWLIMIT   = 0x001
STATUS_REV_HWLIMIT   = 0x002
STATUS_FWD_SWLIMIT   = 0x004
STATUS_REV_SWLIMIT   = 0x008
STATUS_MOVING_FWD    = 0x010
STATUS_MOVING_REV    = 0x020
STATUS_JOGGING_FWD   = 0x040
STATUS_JOGGING_REV   = 0x080
STATUS_MOTOR_CONNECTED = 0x100
STATUS_HOMED          = 0x200

_SDK_DIR = Path(__file__).resolve().parent / "mcm301_sdk"
_DLL_CANDIDATES = [
    _SDK_DIR / "MCM301Lib_x64.dll",
    Path(r"C:\Program Files (x86)\Thorlabs\MCM301\Sample\Thorlabs_MCM301_C++ SDK\MCM301Lib_x64.dll"),
    Path(r"C:\Program Files (x86)\Thorlabs\MCM301\Sample\Thorlabs_MCM301_PythonSDK\MCM301Lib_x64.dll"),
]


class MCM301Error(Exception):
    pass


@dataclass
class DeviceInfo:
    serial: str
    firmware_version: tuple  # (minor, interim, major)
    cpid_version: tuple      # (major, minor)


@dataclass
class AxisStatus:
    slot: int
    position: int          # encoder count
    status_bits: int

    @property
    def at_fwd_limit(self): return bool(self.status_bits & STATUS_FWD_HWLIMIT)
    @property
    def at_rev_limit(self): return bool(self.status_bits & STATUS_REV_HWLIMIT)
    @property
    def moving(self):
        return bool(self.status_bits & (STATUS_MOVING_FWD | STATUS_MOVING_REV |
                                         STATUS_JOGGING_FWD | STATUS_JOGGING_REV))
    @property
    def motor_connected(self): return bool(self.status_bits & STATUS_MOTOR_CONNECTED)
    @property
    def homed(self): return bool(self.status_bits & STATUS_HOMED)


class _StageParamsInfoStruct(ctypes.Structure):
    # min/max_position are DWORD in Thorlabs' header but hold two's-complement
    # negatives on this hardware (a minimum just under 2**32), so signed.
    _fields_ = [("counts_per_unit", c_uint), ("nm_per_count", ctypes.c_float),
                ("minimum_position", c_int), ("maximum_position", c_int),
                ("maximum_speed", ctypes.c_double), ("maximum_acc", ctypes.c_double)]


@dataclass
class StageParams:
    """The stage's own reported parameters (GetStageParams)."""
    counts_per_unit: int
    nm_per_count: float
    minimum_position: int
    maximum_position: int
    maximum_speed: float
    maximum_acc: float


def _find_dll() -> Path:
    for c in _DLL_CANDIDATES:
        if c.exists():
            return c
    raise MCM301Error(
        "MCM301Lib_x64.dll not found. Checked: " +
        ", ".join(str(c) for c in _DLL_CANDIDATES) +
        f". Install the Thorlabs MCM301 software, or copy the DLL into {_SDK_DIR}."
    )


_LIB: ctypes.WinDLL | None = None
_LIB_LOCK = threading.Lock()


# Every DLL entry point used, with its argtypes; all return int. Unpinned,
# ctypes assumes int arguments and silently truncates the pointers.
_SIGNATURES = {
    "List":               [c_char_p, c_int],
    "Open":               [c_char_p, c_int, c_int],
    "IsOpen":             [c_char_p],
    "Close":              [c_int],
    "GetErrorState":      [c_int],
    "GetHardwareInfo":    [c_int, ctypes.c_void_p, c_int, ctypes.c_void_p,
                           c_int],
    "GetSlotDeviceType":  [c_int, c_byte, c_char_p, c_int],
    "GetMotStatus":       [c_int, c_byte, ctypes.POINTER(c_int),
                           ctypes.POINTER(c_uint)],
    "GetStageParams":     [c_int, c_byte,
                           ctypes.POINTER(_StageParamsInfoStruct)],
    "MoveAbsolute":       [c_int, c_byte, c_int],
    "MoveJog":            [c_int, c_byte, c_byte],
    "MoveStop":           [c_int, c_byte],
    "Home":               [c_int, c_byte],
    "SetChanEnableState": [c_int, c_byte, c_byte],
}


def _load_lib() -> ctypes.WinDLL:
    """The vendor DLL, loaded and signature-declared once per process."""
    global _LIB
    with _LIB_LOCK:
        if _LIB is None:
            lib = ctypes.WinDLL(str(_find_dll()))
            for name, argtypes in _SIGNATURES.items():
                fn = getattr(lib, name)
                fn.argtypes = argtypes
                fn.restype = c_int
            _LIB = lib
        return _LIB


def com_name(descriptor: str) -> str:
    """'1313&2016&MCM301&Thorlabs&COM10&COM' -> 'COM10'; the descriptor
    itself if it holds no COM token."""
    m = re.search(r"COM\d+", descriptor.upper())
    return m.group(0) if m else descriptor


def list_devices() -> list[tuple[str, str]]:
    """[(serial_number, com_descriptor), ...] for every MCM301-family device
    Windows sees. Opens no port."""
    lib = _load_lib()
    buf = create_string_buffer(10240)
    n = lib.List(buf, 10240)
    if n < 0:
        raise MCM301Error(f"List() failed (code {n}).")
    # One flat comma list alternating serial and descriptor, with stray empty
    # fields between entries: pair by state, not by index.
    devices: list[tuple[str, str]] = []
    pending_serial: str | None = None
    for field in buf.value.decode("utf-8", "ignore").rstrip("\x00").split(","):
        if pending_serial is None:
            if field:
                pending_serial = field
        else:
            devices.append((pending_serial, field))
            pending_serial = None
    return devices


class MCM301:
    """One open connection to an MCM301 controller."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, timeout_s: int = DEFAULT_TIMEOUT_S):
        self.port_name = port
        self.baud = baud
        self.timeout_s = timeout_s
        self._lib: ctypes.WinDLL | None = None
        self._serial: str | None = None
        self._hdl: int = -1

    # ---- connection -------------------------------------------------------
    def open(self):
        self._lib = _load_lib()
        devices = list_devices()
        if not devices:
            raise MCM301Error(
                "No MCM301 controller is connected to this computer - the "
                "Thorlabs SDK enumerates none. Check that the controller is "
                "powered on and its USB cable is plugged in.")
        # Whole port names: "COM1" is a substring of "...COM10&COM".
        want = self.port_name.upper()
        matches = [sn for sn, com in devices if com_name(com) == want]
        if matches:
            self._serial = matches[0]
        elif len(devices) == 1:
            # Windows renumbers COM ports, even giving one number to two
            # devices (2026-09: COM3 shared with a Bpod; every query blocked
            # forever). The SDK lists only MCM301s, so a lone one is the one
            # meant; record where it really is for logs and metadata.
            self._serial, found_com = devices[0]
            self.port_name = com_name(found_com)
        else:
            raise MCM301Error(
                f"No MCM301 on {self.port_name}, and {len(devices)} are "
                f"connected, so none can be picked unambiguously: {devices!r}")
        hdl = self._lib.Open(self._serial.encode("ascii"), self.baud, self.timeout_s)
        if hdl < 0:
            raise MCM301Error(f"Open({self._serial!r}) failed (code {hdl}).")
        self._hdl = hdl
        if self._lib.IsOpen(self._serial.encode("ascii")) != 1:
            self._hdl = -1
            raise MCM301Error(f"Opened but IsOpen() reports closed for {self._serial!r}.")
        self._verify_responds()

    def _verify_responds(self, budget_s: float = 5.0) -> None:
        """Refuse a controller that opens but never answers.

        A wedged MCM301 (seen after a host process was killed holding the
        port) enumerates and opens cleanly, then the DLL blocks forever,
        ignoring its own timeout; undetected, it hangs the poll worker
        mid-session.
        """
        done = threading.Event()

        def ask():
            try:
                self.get_info()
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=ask, daemon=True).start()
        if done.wait(budget_s):
            return
        # Not Close()d: the thread is still blocked inside the DLL on this
        # handle, and the controller needs a power-cycle regardless.
        self._hdl = -1
        raise MCM301Error(
            f"The MCM301 on {self.port_name} opened but isn't responding "
            f"(no reply within {budget_s:.0f}s). Power-cycle the controller "
            "- switch it off and on, not just the USB cable - then retry.")

    def close(self):
        if self._lib is not None and self._hdl >= 0:
            self._lib.Close(self._hdl)
        self._hdl = -1
        self._lib = None

    @property
    def is_open(self) -> bool:
        return self._hdl >= 0

    def __enter__(self):
        self.open(); return self
    def __exit__(self, *a):
        self.close()

    def _check_open(self):
        if not self.is_open:
            raise MCM301Error("Port isn't open.")

    def _call(self, what: str, fn: str, *args) -> None:
        """self._lib.<fn>(handle, *args), raising `what failed` on an error."""
        self._check_open()
        ret = getattr(self._lib, fn)(self._hdl, *args)
        if ret < 0:
            raise MCM301Error(f"{what} failed (code {ret}).")

    # ---- device info (read-only) -------------------------------------------
    def get_info(self) -> DeviceInfo:
        fw = (c_byte * 3)()
        cpid = (c_byte * 2)()
        self._call("GetHardwareInfo", "GetHardwareInfo", fw, 3, cpid, 2)
        return DeviceInfo(
            serial=self._serial or "",
            firmware_version=tuple(fw),
            cpid_version=tuple(cpid),
        )

    def get_slot_device_type(self, slot: int) -> str:
        buf = create_string_buffer(64)
        self._call(f"GetSlotDeviceType({slot})", "GetSlotDeviceType",
                   slot, buf, 64)
        return buf.value.decode("utf-8", "ignore").rstrip("\x00").replace("\r\n", "")

    def detect_axes(self, slots=(SLOT_X, SLOT_Y, SLOT_Z)) -> list[int]:
        """Return the slots that report a connected motor."""
        found = []
        for slot in slots:
            try:
                if self.get_status(slot).motor_connected:
                    found.append(slot)
            except MCM301Error:
                pass
        return found

    # ---- per-axis reads (read-only) ----------------------------------------
    def get_status(self, slot: int) -> AxisStatus:
        enc = c_int(0)
        bits = c_uint(0)
        self._call(f"GetMotStatus(slot={slot})", "GetMotStatus",
                   slot, byref(enc), byref(bits))
        return AxisStatus(slot=slot, position=enc.value, status_bits=bits.value)

    def get_stage_params(self, slot: int) -> StageParams:
        """The connected stage's own reported scale and travel."""
        info = _StageParamsInfoStruct()
        self._call(f"GetStageParams(slot={slot})", "GetStageParams",
                   slot, byref(info))
        return StageParams(
            counts_per_unit=info.counts_per_unit, nm_per_count=info.nm_per_count,
            minimum_position=info.minimum_position, maximum_position=info.maximum_position,
            maximum_speed=info.maximum_speed, maximum_acc=info.maximum_acc,
        )

    # ======================================================================
    #  MOTION COMMANDS BELOW - these physically move the stage.
    # ======================================================================
    def move_absolute(self, slot: int, target_encoder: int):
        """MOTION: move `slot` to an absolute encoder position."""
        self._call(f"MoveAbsolute(slot={slot})", "MoveAbsolute",
                   slot, int(target_encoder))

    def move_to_readout(self, slot: int, target_readout: int):
        """MOTION: alias for move_absolute(); targets here are encoder counts
        already (no MCM6101-style command units)."""
        self.move_absolute(slot, target_readout)

    def jog_by_readout(self, slot: int, delta_readout: int, current_readout: int | None = None):
        """MOTION: absolute move to current + delta encoder counts (reads
        current if not given)."""
        if current_readout is None:
            current_readout = self.get_status(slot).position
        self.move_to_readout(slot, current_readout + delta_readout)

    def jog(self, slot: int, forward: bool = True):
        """MOTION: start a jog with the controller's own jog params."""
        self._call(f"MoveJog(slot={slot})", "MoveJog",
                   slot, 1 if forward else 0)

    def home(self, slot: int):
        """MOTION: begin a homing move."""
        self._call(f"Home(slot={slot})", "Home", slot)

    def stop(self, slot: int, profiled: bool = True):
        """Stop `slot`. `profiled` is ignored (MCM6101 signature parity): this
        controller has one stop behaviour."""
        self._call(f"MoveStop(slot={slot})", "MoveStop", slot)

    def stop_all(self, slots):
        for s in slots:
            self.stop(s)

    def set_enabled(self, slot: int, enable: bool):
        """Energize or de-energize a stepper."""
        self._call(f"SetChanEnableState(slot={slot})", "SetChanEnableState",
                   slot, 1 if enable else 0)

    # ---- interface parity with the MCM6101 driver --------------------------
    # No command<->encoder scale here; no-ops so StageController.connect()
    # needs no backend branch.
    def set_linear_map(self, slot: int, slope: float, offset: float):
        pass

    def linear_map(self, slot: int) -> tuple[float, float]:
        return (1.0, 0.0)


if __name__ == "__main__":
    # Before the first print: a UnicodeEncodeError from a diagnostic print
    # reads as a device failure (acqApp/console.py).
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    # Read-only self test: identify, then each slot's status. No motion.
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    print(f"Devices seen by the MCM301 SDK: {list_devices()!r}")
    with MCM301(port) as dev:
        info = dev.get_info()
        print(f"Connected on {port}: serial={info.serial} "
              f"firmware={info.firmware_version} cpid={info.cpid_version}")
        for slot, label in ((SLOT_X, "X"), (SLOT_Y, "Y"), (SLOT_Z, "Z")):
            try:
                dtype = dev.get_slot_device_type(slot)
            except MCM301Error as e:
                dtype = f"<error: {e}>"
            try:
                s = dev.get_status(slot)
                flags = [n for n, on in (
                    ("CONNECTED", s.motor_connected), ("HOMED", s.homed),
                    ("MOVING", s.moving), ("FWD_LIM", s.at_fwd_limit),
                    ("REV_LIM", s.at_rev_limit)) if on]
                print(f"  slot {slot} ({label}, {dtype}): pos={s.position:>12} counts "
                      f"[{' '.join(flags)}]")
            except MCM301Error as e:
                print(f"  slot {slot} ({label}, {dtype}): status read failed: {e}")
                continue
            try:
                p = dev.get_stage_params(slot)
                print(f"    stage params: counts_per_unit={p.counts_per_unit} "
                      f"nm_per_count={p.nm_per_count} range=[{p.minimum_position}, "
                      f"{p.maximum_position}] max_speed={p.maximum_speed} "
                      f"max_acc={p.maximum_acc}")
            except MCM301Error as e:
                print(f"    stage params read failed: {e}")
