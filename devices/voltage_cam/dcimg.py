"""DCAM's own recorder — the driver writes frames straight to a `.dcimg`.

Hand-written ctypes: pylablib copies the `DCAMREC_*` enums and the open
structs but binds none of the `dcamrec_*` functions.

Qt-free and acqApp-free; it knows a camera handle and a path, nothing else.
Bind order is fixed by DCAM: allocate the buffer, `attach()`, *then* start
capture. Attaching a recorder to a running capture is not allowed.
"""
from __future__ import annotations

import ctypes
import shutil
from dataclasses import dataclass
from pathlib import Path

_int32 = ctypes.c_int32

# DCAMREC_STATUSFLAG_RECORDING
FLAG_RECORDING = 0x01

# A recording's length isn't known when it opens (the operator presses Stop),
# but `maxframepersession` must be a real cap: 0 is rejected outright, and
# `dcamcap_record` fails with FAILEDWRITEDATA once cap x frame_bytes exceeds
# the target drive's FREE SPACE (measured 2026-09-23: 1e6 frames of 128 KB
# bound on D: with 678 GB free, failed on C:; 1e7 failed on both). So the cap
# is computed per recording, not fixed.
DISK_FRACTION = 0.9         # leave the drive some headroom
MIN_FRAMES = 16             # refuse to open a recording that can't hold a burst


def frames_that_fit(path: Path | str, frame_bytes: int,
                    fraction: float = DISK_FRACTION) -> int:
    """Largest `maxframepersession` the drive holding `path` will accept."""
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    p = Path(path)
    probe = p if p.exists() else p.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    return max(0, int(free * fraction) // frame_bytes)


class DcimgError(RuntimeError):
    """A dcamrec_* call failed. Carries DCAM's own error name where known."""


class DCAMREC_STATUS(ctypes.Structure):
    """dcamapi4.h. pylablib has the open structs but not this one."""
    _fields_ = [("size", _int32),
                ("currentsession_index", _int32),
                ("maxframecount_per_session", _int32),
                ("currentframe_index", _int32),
                ("missingframe_count", _int32),
                ("flags", _int32),
                ("totalframecount", _int32),
                ("reserved", _int32)]


@dataclass(frozen=True)
class RecStatus:
    """What the recorder says it has written."""
    total: int          # frames in the file
    index: int          # newest frame's index
    missing: int        # frames the recorder never got — real data loss
    recording: bool
    session: int = 0    # which session within the file; see MAX_FRAMES


def _err_name(code: int) -> str:
    try:
        from pylablib.devices.DCAM.dcamapi4_defs import drDCAMERR
        return drDCAMERR.get(code, f"0x{code & 0xFFFFFFFF:08X}")
    except Exception:                       # noqa: BLE001 — naming is a nicety
        return f"0x{code & 0xFFFFFFFF:08X}"


def _check(code: int, call: str) -> None:
    """DCAMERR is negative on failure, >= 1 on success."""
    if code < 0:
        raise DcimgError(f"{call} failed: {_err_name(code)}")


def available() -> bool:
    """Whether this machine's dcamapi exports the recorder API at all."""
    try:
        dll = ctypes.windll.dcamapi
    except (OSError, AttributeError):
        return False
    return all(hasattr(dll, fn) for fn in
               ("dcamrec_openW", "dcamcap_record", "dcamrec_status",
                "dcamrec_close"))


class DcimgRecorder:
    """One `.dcimg` file, from `open()` to `close()`.

        rec = DcimgRecorder(path)
        rec.open()
        cam.setup_acquisition(...)   # buffer must exist first
        rec.attach(cam.handle)
        cam.start_acquisition()
        ...
        cam.stop_acquisition()       # stop capture BEFORE closing the file
        rec.close()
    """

    @classmethod
    def for_frames(cls, path: Path | str, frame_bytes: int) -> DcimgRecorder:
        """Sized to what the target drive can actually take."""
        n = frames_that_fit(path, frame_bytes)
        if n < MIN_FRAMES:
            raise DcimgError(
                f"no room for a .dcimg on {Path(path).drive or Path(path)}: "
                f"fits {n} frames of {frame_bytes / 1e6:.1f} MB")
        return cls(path, max_frames=n)

    def __init__(self, path: Path | str, max_frames: int) -> None:
        # DCAM appends the extension itself, so hand it the stem.
        p = Path(path)
        self.path = p if p.suffix == ".dcimg" else p.with_suffix(".dcimg")
        self._stem = str(self.path.with_suffix(""))
        self._max_frames = int(max_frames)
        self._dll = ctypes.windll.dcamapi
        self._hrec: ctypes.c_void_p | None = None
        self._hdcam: int | None = None

    @property
    def is_open(self) -> bool:
        return self._hrec is not None

    @property
    def max_frames(self) -> int:
        return self._max_frames

    def open(self) -> None:
        # Guards before the bindings: these are the two mistakes worth
        # catching without a camera present.
        if self._hrec is not None:
            raise DcimgError("already open")
        if self._max_frames < 1:
            raise DcimgError("max_frames must be positive (0 is rejected by "
                             "dcamrec_openW, it does not mean unlimited)")
        from pylablib.devices.DCAM.dcamapi4_defs import DCAMREC_OPENW
        self.path.parent.mkdir(parents=True, exist_ok=True)
        op = DCAMREC_OPENW()
        op.size = ctypes.sizeof(DCAMREC_OPENW)
        op.path = self._stem
        op.ext = "dcimg"
        op.maxframepersession = self._max_frames
        _check(self._dll.dcamrec_openW(ctypes.byref(op)), "dcamrec_openW")
        self._hrec = ctypes.c_void_p(op.hrec)

    def attach(self, hdcam: int) -> None:
        """Bind to a camera whose buffer is allocated but capture not started."""
        if self._hrec is None:
            raise DcimgError("not open")
        _check(self._dll.dcamcap_record(ctypes.c_void_p(hdcam), self._hrec),
               "dcamcap_record")
        self._hdcam = hdcam

    def detach(self) -> None:
        """Unbind, leaving the camera free to capture to memory again. Capture
        must already be stopped."""
        if self._hdcam is None:
            return
        try:
            _check(self._dll.dcamcap_record(ctypes.c_void_p(self._hdcam), None),
                   "dcamcap_record(NULL)")
        finally:
            self._hdcam = None

    def status(self) -> RecStatus:
        if self._hrec is None:
            raise DcimgError("not open")
        st = DCAMREC_STATUS()
        st.size = ctypes.sizeof(DCAMREC_STATUS)
        _check(self._dll.dcamrec_status(self._hrec, ctypes.byref(st)),
               "dcamrec_status")
        return RecStatus(total=int(st.totalframecount),
                         index=int(st.currentframe_index),
                         missing=int(st.missingframe_count),
                         recording=bool(st.flags & FLAG_RECORDING),
                         session=int(st.currentsession_index))

    def close(self) -> None:
        """Idempotent, and never raises: it runs on the failure path too."""
        if self._hdcam is not None:
            try:
                self.detach()
            except DcimgError as e:
                print(f"[dcimg] detach failed: {e}")
        if self._hrec is None:
            return
        code = self._dll.dcamrec_close(self._hrec)
        self._hrec = None
        if code < 0:
            print(f"[dcimg] dcamrec_close failed: {_err_name(code)}")

    def __enter__(self) -> DcimgRecorder:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
