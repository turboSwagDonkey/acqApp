"""
mcm301_driver.py - Minimal, safe driver for a Thorlabs MCM301 3-channel
stepper-motor stage controller: the replacement for the MCM6101 this app
previously drove (see driver.py / docs/STAGE_TRANSFER.md for that hardware).

Unlike the MCM6101, the MCM301 does not speak APT over a raw USB-CDC serial
port. Thorlabs drives it through a vendor DLL (MCM301Lib_x64.dll) that owns
the serial framing internally, and that DLL is the only documented
interface -- so this driver wraps it via ctypes rather than reimplementing
an undocumented wire protocol.

Verified on this hardware:
  * Connection : USB CDC. It enumerated as COM3 until Windows handed that
                 number to a second device as well, which wedged it (see
                 open()); it now has COM10 to itself. The controller is
                 identified by SERIAL NUMBER, so the port number is advisory.
  * Addressing : each installed stepper card lives in a FIXED controller
                 slot -- 4, 5, 6 (not axis index 0,1,2 like the old driver).
  * Axes       : slots 4 and 5 hold MMP-201121 stages (0.5 um/count, travel
                 +-50800 counts); slot 6 holds a PLS-283529, left unexercised.

Every method that causes MOTION is clearly marked. Nothing moves unless you
call one of those methods.
"""
from __future__ import annotations
import ctypes
import re
import threading
from ctypes import c_int, c_byte, c_uint, c_char_p, create_string_buffer, byref
from dataclasses import dataclass
from pathlib import Path

# Fixed slot numbers for this controller (per Thorlabs MCM301 SDK docs).
SLOT_X = 4
SLOT_Y = 5
SLOT_Z = 6  # present on the hardware; not driven by this app yet

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
    # minimum/maximum_position are declared DWORD (unsigned) in Thorlabs'
    # header, but the values observed on this hardware are two's-complement
    # negative numbers (e.g. a homed stage centered at 0 reports a minimum a
    # large unsigned value just under 2**32) -- so these are read as signed.
    _fields_ = [("counts_per_unit", c_uint), ("nm_per_count", ctypes.c_float),
                ("minimum_position", c_int), ("maximum_position", c_int),
                ("maximum_speed", ctypes.c_double), ("maximum_acc", ctypes.c_double)]


@dataclass
class StageParams:
    """The physical stage's own reported parameters (GetStageParams) -- real
    calibration data straight from the controller, unlike the MCM6101's
    command-unit scale which had to be measured by hand."""
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


def _load_lib() -> ctypes.WinDLL:
    """The vendor DLL, loaded and signature-declared once. Windows refcounts
    the module anyway, but re-declaring twenty signatures on every call is
    pure waste — a single connect() would otherwise do it three times."""
    global _LIB
    with _LIB_LOCK:
        if _LIB is not None:
            return _LIB
        lib = ctypes.WinDLL(str(_find_dll()))
        _declare(lib)
        _LIB = lib
        return lib


def _declare(lib: ctypes.WinDLL) -> None:
    """Pin every signature this driver calls. ctypes otherwise assumes int
    arguments and an int return, which silently truncates the pointers."""
    lib.List.argtypes = [c_char_p, c_int]
    lib.List.restype = c_int
    lib.Open.argtypes = [c_char_p, c_int, c_int]
    lib.Open.restype = c_int
    lib.IsOpen.argtypes = [c_char_p]
    lib.IsOpen.restype = c_int
    lib.Close.argtypes = [c_int]
    lib.Close.restype = c_int
    lib.GetErrorState.argtypes = [c_int]
    lib.GetErrorState.restype = c_int
    lib.GetHardwareInfo.argtypes = [c_int, ctypes.c_void_p, c_int, ctypes.c_void_p, c_int]
    lib.GetHardwareInfo.restype = c_int
    lib.GetSlotDeviceType.argtypes = [c_int, c_byte, c_char_p, c_int]
    lib.GetSlotDeviceType.restype = c_int
    lib.GetMotStatus.argtypes = [c_int, c_byte, ctypes.POINTER(c_int), ctypes.POINTER(c_uint)]
    lib.GetMotStatus.restype = c_int
    lib.GetStageParams.argtypes = [c_int, c_byte, ctypes.POINTER(_StageParamsInfoStruct)]
    lib.GetStageParams.restype = c_int
    lib.MoveAbsolute.argtypes = [c_int, c_byte, c_int]
    lib.MoveAbsolute.restype = c_int
    lib.MoveJog.argtypes = [c_int, c_byte, c_byte]
    lib.MoveJog.restype = c_int
    lib.MoveStop.argtypes = [c_int, c_byte]
    lib.MoveStop.restype = c_int
    lib.Home.argtypes = [c_int, c_byte]
    lib.Home.restype = c_int
    lib.SetChanEnableState.argtypes = [c_int, c_byte, c_byte]
    lib.SetChanEnableState.restype = c_int


def com_name(descriptor: str) -> str:
    """Pull the plain port name out of an enumeration descriptor, e.g.
    '1313&2016&MCM301&Thorlabs&COM10&COM' -> 'COM10'. Falls back to the whole
    descriptor if it holds no recognizable COM token."""
    m = re.search(r"COM\d+", descriptor.upper())
    return m.group(0) if m else descriptor


def list_devices() -> list[tuple[str, str]]:
    """Return [(serial_number, com_descriptor), ...] for every MCM301-family
    device Windows currently sees (open or not). Read-only; no port is opened."""
    lib = _load_lib()
    buf = create_string_buffer(10240)
    n = lib.List(buf, 10240)
    if n < 0:
        raise MCM301Error(f"List() failed (code {n}).")
    # The DLL returns one flat comma-separated list, alternating serial number
    # and port descriptor. Empty fields do appear between entries, so pair them
    # up by state rather than by index.
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
        # Compare the extracted port name, not a substring of the descriptor:
        # "COM1" is a substring of "…COM10&COM" and would match the wrong box.
        want = self.port_name.upper()
        matches = [sn for sn, com in devices if com_name(com) == want]
        if matches:
            self._serial = matches[0]
        elif len(devices) == 1:
            # Windows reassigns COM numbers freely — and will even hand the
            # same number to two devices if one of them was unplugged at the
            # time, which routes this controller's traffic to whatever else
            # claimed it (seen 2026-09: COM3 shared with a Bpod interface;
            # every query then blocked forever). The SDK's enumeration lists
            # ONLY MCM301-family devices, so a single unambiguous controller
            # is the one meant, whatever number it landed on. Record where it
            # actually was, so logs and metadata don't lie.
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
        """Confirm the controller actually answers before we call it connected.

        A wedged MCM301 still enumerates and still opens cleanly, but then
        answers nothing -- and the vendor DLL does NOT honour its own open
        timeout there, it blocks forever. Observed after a host process was
        killed while holding the port. Left undetected that hangs the position
        poll worker mid-session, which is far worse than refusing to start.
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
        # Deliberately NOT Close()d: that thread is still blocked inside the
        # DLL, and closing the handle out from under it is not safe. The
        # controller needs a power-cycle regardless.
        self._hdl = -1
        raise MCM301Error(
            f"The MCM301 on {self.port_name} opened but is not responding "
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
            raise MCM301Error("Port is not open.")

    # ---- device info (read-only) -------------------------------------------
    def get_info(self) -> DeviceInfo:
        self._check_open()
        fw = (c_byte * 3)()
        cpid = (c_byte * 2)()
        ret = self._lib.GetHardwareInfo(self._hdl, fw, 3, cpid, 2)
        if ret < 0:
            raise MCM301Error(f"GetHardwareInfo failed (code {ret}).")
        return DeviceInfo(
            serial=self._serial or "",
            firmware_version=tuple(fw),
            cpid_version=tuple(cpid),
        )

    def get_slot_device_type(self, slot: int) -> str:
        self._check_open()
        buf = create_string_buffer(64)
        ret = self._lib.GetSlotDeviceType(self._hdl, slot, buf, 64)
        if ret < 0:
            raise MCM301Error(f"GetSlotDeviceType({slot}) failed (code {ret}).")
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
        self._check_open()
        enc = c_int(0)
        bits = c_uint(0)
        ret = self._lib.GetMotStatus(self._hdl, slot, byref(enc), byref(bits))
        if ret < 0:
            raise MCM301Error(f"GetMotStatus(slot={slot}) failed (code {ret}).")
        return AxisStatus(slot=slot, position=enc.value, status_bits=bits.value)

    def get_stage_params(self, slot: int) -> StageParams:
        """The connected stage's own reported scale and travel -- real
        numbers from the controller, not something this app has to measure."""
        self._check_open()
        info = _StageParamsInfoStruct()
        ret = self._lib.GetStageParams(self._hdl, slot, byref(info))
        if ret < 0:
            raise MCM301Error(f"GetStageParams(slot={slot}) failed (code {ret}).")
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
        self._check_open()
        ret = self._lib.MoveAbsolute(self._hdl, slot, int(target_encoder))
        if ret < 0:
            raise MCM301Error(f"MoveAbsolute(slot={slot}) failed (code {ret}).")

    def move_to_readout(self, slot: int, target_readout: int):
        """MOTION: alias for move_absolute(). Unlike the MCM6101, this
        controller's move target IS the encoder count directly -- there is no
        coarser command-unit scale to convert through first."""
        self.move_absolute(slot, target_readout)

    def jog_by_readout(self, slot: int, delta_readout: int, current_readout: int | None = None):
        """MOTION: jog `slot` by delta encoder counts, implemented as an
        absolute move to (current + delta). Reads current position if not
        supplied."""
        if current_readout is None:
            current_readout = self.get_status(slot).position
        self.move_to_readout(slot, current_readout + delta_readout)

    def jog(self, slot: int, forward: bool = True):
        """MOTION: start a jog move in one direction (controller's own jog params)."""
        self._check_open()
        ret = self._lib.MoveJog(self._hdl, slot, 1 if forward else 0)
        if ret < 0:
            raise MCM301Error(f"MoveJog(slot={slot}) failed (code {ret}).")

    def home(self, slot: int):
        """MOTION: begin a homing move."""
        self._check_open()
        ret = self._lib.Home(self._hdl, slot)
        if ret < 0:
            raise MCM301Error(f"Home(slot={slot}) failed (code {ret}).")

    def stop(self, slot: int, profiled: bool = True):
        """Stop motion on `slot`. `profiled` is accepted (and ignored) for
        signature parity with the MCM6101 driver -- this controller has a
        single stop behaviour."""
        self._check_open()
        ret = self._lib.MoveStop(self._hdl, slot)
        if ret < 0:
            raise MCM301Error(f"MoveStop(slot={slot}) failed (code {ret}).")

    def stop_all(self, slots):
        for s in slots:
            self.stop(s)

    def set_enabled(self, slot: int, enable: bool):
        """Enable (energize) or disable (de-energize) a stepper."""
        self._check_open()
        ret = self._lib.SetChanEnableState(self._hdl, slot, 1 if enable else 0)
        if ret < 0:
            raise MCM301Error(f"SetChanEnableState(slot={slot}) failed (code {ret}).")

    # ---- interface parity with the MCM6101 driver --------------------------
    # StageController.connect() loads a saved slope/offset into the driver
    # when one is on file. This hardware has no command<->encoder scale to
    # store -- move_to_readout() already targets true encoder counts -- so
    # these are no-ops, present only so that code path doesn't need a
    # backend-specific branch.
    def set_linear_map(self, slot: int, slope: float, offset: float):
        pass

    def linear_map(self, slot: int) -> tuple[float, float]:
        return (1.0, 0.0)


if __name__ == "__main__":
    # Make the console unable to raise on this script's own output before it
    # prints anything (see acqApp/console.py -- a UnicodeEncodeError from a
    # diagnostic print is otherwise indistinguishable from a device failure).
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    # Read-only self test: connect, identify device, print X/Y status. No motion.
    import sys
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
