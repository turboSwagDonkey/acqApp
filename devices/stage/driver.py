"""Driver for a Thorlabs MCM6101 / MCM6000-series stage controller over its
USB CDC serial port (Thorlabs APT message set).

Verified on this hardware:
  * Port      : USB CDC, 115200 8N1, DTR+RTS asserted.
  * Controller: destination 0x11 answers HW_REQ_INFO (model "MCM61010").
  * Axes      : axis N (0-indexed) is destination 0x21 + N, with the message's
                channel-ident field also N; axes 0-2 answer. The rig's chip
                numbers are 1-indexed: chip N = axis N-1.

Only methods marked MOTION move anything.
"""
from __future__ import annotations
import struct
import time
import threading
from dataclasses import dataclass

import serial

HOST = 0x01
CONTROLLER = 0x11
BAY0 = 0x21  # axis N lives at destination BAY0 + N

# APT message IDs
MGMSG_HW_REQ_INFO          = 0x0005
MGMSG_HW_GET_INFO          = 0x0006
MGMSG_MOD_IDENTIFY         = 0x0223
MGMSG_MOD_SET_CHANENABLESTATE = 0x0210
# NOTE: this firmware uses 1=enable / 0=disable (not the standard APT 1/2).
CHAN_ENABLE  = 0x01
CHAN_DISABLE = 0x00
MGMSG_MOT_MOVE_HOME        = 0x0443
MGMSG_MOT_MOVE_HOMED       = 0x0444
MGMSG_MOT_MOVE_ABSOLUTE    = 0x0453  # long form (with data packet)
MGMSG_MOT_MOVE_RELATIVE    = 0x0448  # long form (with data packet)
MGMSG_MOT_MOVE_COMPLETED   = 0x0464
MGMSG_MOT_MOVE_STOP        = 0x0465
MGMSG_MOT_MOVE_STOPPED     = 0x0466
MGMSG_MOT_MOVE_JOG         = 0x046A
MGMSG_MOT_REQ_POSCOUNTER   = 0x0411
MGMSG_MOT_GET_POSCOUNTER   = 0x0412
MGMSG_MOT_REQ_STATUSUPDATE = 0x0480
MGMSG_MOT_GET_STATUSUPDATE = 0x0481

# Mirror/light-path (Slider_IO_type) cards, e.g. chip 7's PMT/camera switch:
# not steppers, so MOT_REQ_STATUSUPDATE never answers them. IDs from
# ThorImageLS's ThorMCM6000 driver source (APT.h/APT.cpp).
MGMSG_MCM_SET_MIRROR_STATE = 0x4087
MGMSG_MCM_REQ_MIRROR_STATE = 0x4088
MGMSG_MCM_GET_MIRROR_STATE = 0x4089

MIRROR_OUT = 0
MIRROR_IN = 1
MIRROR_UNKNOWN = 2

# Channels on a mirror card (Mcm6kParams.h). The scan head is galvo-resonant
# only (operator-confirmed 2026-09-11), so GG never applies here.
MIRROR_CHAN_GG = 4
MIRROR_CHAN_GR = 5
MIRROR_CHAN_CAMERA = 6

# status-bits (subset, from APT spec)
STATUS_FWD_HWLIMIT = 0x00000001
STATUS_REV_HWLIMIT = 0x00000002
STATUS_MOVING_FWD  = 0x00000010
STATUS_MOVING_REV  = 0x00000020
STATUS_HOMING      = 0x00000200
STATUS_HOMED       = 0x00000400
STATUS_ENABLED     = 0x80000000

JOG_FORWARD = 1
JOG_REVERSE = 2
STOP_IMMEDIATE = 1
STOP_PROFILED = 2

# MOTION: past any real axis's travel, so the reverse hard limit stops it first.
REVERSE_LIMIT_SEEK_COUNTS = -30_000_000


@dataclass
class AxisStatus:
    index: int
    position: int          # encoder counts
    enc_count: int
    status_bits: int

    @property
    def enabled(self):     return bool(self.status_bits & STATUS_ENABLED)
    @property
    def homed(self):       return bool(self.status_bits & STATUS_HOMED)
    @property
    def homing(self):      return bool(self.status_bits & STATUS_HOMING)
    @property
    def moving(self):      return bool(self.status_bits & (STATUS_MOVING_FWD | STATUS_MOVING_REV))
    @property
    def at_fwd_limit(self): return bool(self.status_bits & STATUS_FWD_HWLIMIT)
    @property
    def at_rev_limit(self): return bool(self.status_bits & STATUS_REV_HWLIMIT)


@dataclass
class DeviceInfo:
    serial: int
    model: str
    firmware: str


class MCM6101Error(Exception):
    pass


class MCM6101:
    # Absolute moves take coarser "command units" than the encoder readout;
    # measured readout ~= 17.78 * command on the XY axes.
    DEFAULT_SCALE = 17.78

    def __init__(self, port: str, timeout: float = 0.5, default_scale: float = DEFAULT_SCALE):
        self.port_name = port
        self.timeout = timeout
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self.default_scale = default_scale
        self._scale: dict[int, float] = {}   # axis -> readout counts per command unit
        # Per-axis encoder = slope * command + offset. A hard-limit hit
        # re-references the command origin, so `offset` goes stale with it.
        self._slope: dict[int, float] = {}
        self._offset: dict[int, float] = {}

    def set_scale(self, axis: int, readout_per_cmd: float):
        self._scale[axis] = float(readout_per_cmd)

    def scale(self, axis: int) -> float:
        return self._scale.get(axis, self.default_scale)

    def set_linear_map(self, axis: int, slope: float, offset: float):
        self._slope[axis] = float(slope)
        self._offset[axis] = float(offset)

    def linear_map(self, axis: int) -> tuple[float, float]:
        return (self._slope.get(axis, self.scale(axis)),
                self._offset.get(axis, 0.0))

    # ---- connection -------------------------------------------------------
    def open(self):
        self._ser = serial.Serial(self.port_name, baudrate=115200, bytesize=8,
                                   parity="N", stopbits=1, timeout=self.timeout)
        # MCM6101 is a USB-CDC device; it only transmits with DTR asserted.
        self._ser.dtr = True
        self._ser.rts = True
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def __enter__(self):
        self.open(); return self
    def __exit__(self, *a):
        self.close()

    # ---- low-level framing ------------------------------------------------
    @staticmethod
    def _header(cmd, p1=0, p2=0, dest=CONTROLLER, src=HOST):
        return bytes([cmd & 0xFF, (cmd >> 8) & 0xFF, p1 & 0xFF, p2 & 0xFF,
                      dest & 0xFF, src & 0xFF])

    @staticmethod
    def _header_with_data(cmd, data: bytes, dest=CONTROLLER, src=HOST):
        n = len(data)
        return bytes([cmd & 0xFF, (cmd >> 8) & 0xFF, n & 0xFF, (n >> 8) & 0xFF,
                      (dest | 0x80) & 0xFF, src & 0xFF]) + data

    def _write(self, pkt: bytes):
        if not self.is_open:
            raise MCM6101Error("Port isn't open.")
        self._ser.write(pkt)
        self._ser.flush()

    def _read_exact(self, n: int, deadline: float) -> bytes | None:
        """Exactly n bytes before `deadline`, or None."""
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            self._ser.timeout = remaining
            chunk = self._ser.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _read_message(self, want_cmd: int, wait: float = 0.4) -> bytes | None:
        """Read whole frames until one is `want_cmd` or `wait` s pass. Reading
        each frame's declared length returns on arrival, not on the serial
        timeout."""
        deadline = time.time() + wait
        while time.time() < deadline:
            header = self._read_exact(6, deadline)
            if header is None:
                return None
            cmd = header[0] | (header[1] << 8)
            has_data = bool(header[4] & 0x80)
            dlen = (header[2] | (header[3] << 8)) if has_data else 0
            data = b""
            if dlen:
                data = self._read_exact(dlen, deadline)
                if data is None:
                    return None
            if cmd == want_cmd:
                return header + data
        return None

    def _query(self, cmd: int, reply: int, dest: int, p1: int = 0,
               wait: float = 0.4) -> bytes | None:
        with self._lock:
            self._ser.reset_input_buffer()
            self._write(self._header(cmd, p1=p1, dest=dest))
            return self._read_message(reply, wait=wait)

    # ---- device info (read-only) -----------------------------------------
    def get_info(self) -> DeviceInfo:
        msg = self._query(MGMSG_HW_REQ_INFO, MGMSG_HW_GET_INFO, CONTROLLER,
                          wait=0.6)
        if not msg or len(msg) < 90:
            raise MCM6101Error("No/short HW_GET_INFO reply from controller.")
        body = msg[6:]
        serial_no = struct.unpack_from("<I", body, 0)[0]
        model = body[4:12].split(b"\x00")[0].decode("ascii", "replace")
        fw = body[14:18]
        return DeviceInfo(serial_no, model, f"{fw[2]}.{fw[1]}.{fw[0]}")

    # ---- per-axis reads (read-only) --------------------------------------
    def get_position(self, axis: int) -> int:
        msg = self._query(MGMSG_MOT_REQ_POSCOUNTER, MGMSG_MOT_GET_POSCOUNTER,
                          BAY0 + axis, p1=axis)
        if not msg or len(msg) < 12:
            raise MCM6101Error(f"No position reply from axis {axis}.")
        return struct.unpack_from("<i", msg, 8)[0]

    def get_status(self, axis: int, wait: float = 0.4) -> AxisStatus:
        msg = self._query(MGMSG_MOT_REQ_STATUSUPDATE,
                          MGMSG_MOT_GET_STATUSUPDATE, BAY0 + axis, p1=axis,
                          wait=wait)
        if not msg or len(msg) < 20:
            raise MCM6101Error(f"No status reply from axis {axis}.")
        chan = struct.unpack_from("<H", msg, 6)[0]
        pos = struct.unpack_from("<i", msg, 8)[0]
        enc = struct.unpack_from("<i", msg, 12)[0]
        bits = struct.unpack_from("<I", msg, 16)[0]
        return AxisStatus(chan, pos, enc, bits)

    def get_mirror_state(self, axis: int, channel: int, wait: float = 0.4) -> int:
        """One mirror-card channel (chip 7 = axis 6 here): MIRROR_OUT, _IN or
        _UNKNOWN. Read-only."""
        msg = self._query(MGMSG_MCM_REQ_MIRROR_STATE,
                          MGMSG_MCM_GET_MIRROR_STATE, BAY0 + axis, p1=channel,
                          wait=wait)
        if not msg or len(msg) < 4:
            raise MCM6101Error(f"No mirror-state reply from axis {axis} channel {channel}.")
        return msg[3]

    def detect_axes(self, max_axes: int = 6, stop_after_misses: int = 2) -> list[int]:
        """Axes that answer a status request, stopping after
        `stop_after_misses` consecutive silent ones."""
        found = []
        misses = 0
        for a in range(max_axes):
            try:
                self.get_status(a, wait=0.2)
                found.append(a)
                misses = 0
            except MCM6101Error:
                misses += 1
                if misses >= stop_after_misses:
                    break
        return found

    def _send(self, cmd: int, axis: int | None, data: bytes | None = None,
              **kw) -> None:
        """Write one command to `axis`'s bay (None = the controller)."""
        dest = CONTROLLER if axis is None else BAY0 + axis
        pkt = (self._header_with_data(cmd, data, dest=dest) if data is not None
               else self._header(cmd, dest=dest, **kw))
        with self._lock:
            self._write(pkt)

    def set_enabled(self, axis: int, enable: bool):
        """Energize or de-energize an axis. A disabled axis ignores moves and
        isn't held, so it may back-drive."""
        self._send(MGMSG_MOD_SET_CHANENABLESTATE, axis, p1=axis,
                   p2=CHAN_ENABLE if enable else CHAN_DISABLE)

    def is_enabled(self, axis: int) -> bool:
        return self.get_status(axis).enabled

    def identify(self, axis: int | None = None):
        """Flash an LED (safe, no motion). axis=None flashes the controller."""
        self._send(MGMSG_MOD_IDENTIFY, axis, p1=0 if axis is None else axis)

    # ======================================================================
    #  MOTION COMMANDS BELOW - these physically move the stage.
    # ======================================================================
    def move_relative(self, axis: int, delta_cmd: int):
        """MOTION: relative move in COMMAND units. This firmware (MCM61010 fw
        7.0.2) ignores MOVE_RELATIVE; use jog_by_readout()."""
        self._send(MGMSG_MOT_MOVE_RELATIVE, axis,
                   struct.pack("<Hi", axis, int(delta_cmd)))

    def move_absolute(self, axis: int, position_cmd: int):
        """MOTION: absolute move in COMMAND units; most callers want
        move_to_readout()."""
        self._send(MGMSG_MOT_MOVE_ABSOLUTE, axis,
                   struct.pack("<Hi", axis, int(position_cmd)))

    def move_to_readout(self, axis: int, target_readout: int):
        """MOTION: absolute move in READOUT (encoder) counts, via
        command = (enc - offset) / slope."""
        slope, offset = self.linear_map(axis)
        cmd = int(round((target_readout - offset) / slope))
        self.move_absolute(axis, cmd)

    def jog_by_readout(self, axis: int, delta_readout: int, current_readout: int | None = None):
        """MOTION: absolute move to current + delta READOUT counts (reads
        current if not given)."""
        if current_readout is None:
            current_readout = self.get_status(axis).position
        self.move_to_readout(axis, current_readout + delta_readout)

    def jog(self, axis: int, forward: bool = True):
        """MOTION: start a jog with the controller's own jog params."""
        self._send(MGMSG_MOT_MOVE_JOG, axis, p1=axis,
                   p2=JOG_FORWARD if forward else JOG_REVERSE)

    def home(self, axis: int):
        """MOTION: home the axis to its reference/limit."""
        self._send(MGMSG_MOT_MOVE_HOME, axis, p1=axis)

    def set_mirror_state(self, axis: int, channel: int, state: int):
        """MOTION: flip a mirror-card channel to MIRROR_OUT or MIRROR_IN."""
        self._send(MGMSG_MCM_SET_MIRROR_STATE, axis, p1=channel, p2=state)

    # ---- frame establishment (for absolute positioning) -------------------
    def wait_stopped(self, axis: int, timeout: float = 20.0, tol: int = 20) -> int:
        """Block until the axis stops and its position is stable; return it."""
        time.sleep(0.3)  # let the move start
        t0 = time.time()
        last = None
        stable = 0
        while time.time() - t0 < timeout:
            s = self.get_status(axis)
            if not s.moving and last is not None and abs(s.position - last) < tol:
                stable += 1
                if stable >= 3:
                    return s.position
            else:
                stable = 0
            last = s.position
            time.sleep(0.15)
        return self.get_status(axis).position

    def drive_to_reverse_limit(self, axis: int, timeout: float = 150) -> int:
        """MOTION: drive to the reverse hard limit; return its encoder value.
        Re-references the command origin."""
        self.move_absolute(axis, REVERSE_LIMIT_SEEK_COUNTS)
        time.sleep(0.4)
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.get_status(axis)
            if s.at_rev_limit and not s.moving:
                return s.position
            time.sleep(0.2)
        return self.get_status(axis).position

    def establish_frame(self, axis: int, span_counts: int,
                        probe_a: int = 20000, probe_b: int = 40000) -> dict:
        """MOTION: drive to the reverse limit, then measure and store the
        command->encoder map. Returns {R, slope, offset, travel_min,
        travel_max, true_center} in encoder counts; travel = [R, R+span],
        since positive command drives away from the reverse limit."""
        R = self.drive_to_reverse_limit(axis)
        self.move_absolute(axis, probe_a); e1 = self.wait_stopped(axis)
        self.move_absolute(axis, probe_b); e2 = self.wait_stopped(axis)
        if abs(e2 - e1) < 1000:
            raise MCM6101Error(f"Axis {axis}: frame probe saturated "
                               f"(e1={e1}, e2={e2}); could not measure slope.")
        slope = (e2 - e1) / (probe_b - probe_a)
        offset = e1 - slope * probe_a
        # ~17.8 enc/cmd on this hardware; anything wild is a bad read (still
        # moving, or a limit hit mid-probe).
        if not (10.0 < abs(slope) < 25.0):
            raise MCM6101Error(f"Axis {axis}: implausible frame slope {slope:.2f} "
                               f"(e1={e1}, e2={e2}); try again.")
        self.set_linear_map(axis, slope, offset)
        return {"R": R, "slope": slope, "offset": offset,
                "travel_min": R, "travel_max": R + span_counts,
                "true_center": R + span_counts // 2}

    def stop(self, axis: int, profiled: bool = True):
        """Stop an axis (profiled = controlled deceleration)."""
        self._send(MGMSG_MOT_MOVE_STOP, axis, p1=axis,
                   p2=STOP_PROFILED if profiled else STOP_IMMEDIATE)

    def stop_all(self, axes):
        for a in axes:
            self.stop(a, profiled=False)


if __name__ == "__main__":
    # Before the first print: a UnicodeEncodeError from a diagnostic print
    # reads as a device failure (acqApp/console.py).
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()

    # Read-only self test: identify, list axes and positions. No motion.
    port = sys.argv[1] if len(sys.argv) > 1 else "COM54"
    with MCM6101(port) as dev:
        info = dev.get_info()
        print(f"Connected: model={info.model} serial={info.serial} fw={info.firmware}")
        axes = dev.detect_axes()
        print(f"Axes detected: {axes}")
        for a in axes:
            s = dev.get_status(a)
            flags = [n for n, on in (("EN", s.enabled), ("HOMED", s.homed),
                     ("MOVING", s.moving), ("FWD_LIM", s.at_fwd_limit),
                     ("REV_LIM", s.at_rev_limit)) if on]
            print(f"  axis {a}: pos={s.position:>12} counts  [{' '.join(flags)}]")
