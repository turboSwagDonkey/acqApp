"""Where a session file goes — the model, and no Qt.

The filename template is operator free text, so `{subject}` alone resolves every
recording of the day to one path; `resolve(unique=True)` is what stops the
second truncating the first. `tests/test_save_paths.py` drives this directly.
"""
from __future__ import annotations

import os
import re
import shutil
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# Windows-invalid filename characters, plus control chars.
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEFAULT_SUBDIR = "acq_sessions"

TOKENS = ("{subject}", "{session}", "{date}", "{time}")


def sanitize(name: str, fallback: str = "session") -> str:
    """Make `name` safe as a single path component."""
    cleaned = _BAD.sub("_", (name or "").strip()).strip(" .")
    return cleaned or fallback


def list_drives() -> list[tuple[str, int, int]]:
    """[(root, free_bytes, total_bytes)] for every readable fixed drive.

    Sorted by free space descending — the drive with room is the one you want,
    and it is rarely the system drive.
    """
    roots: list[str] = []
    if os.name == "nt":
        roots = [f"{d}:\\" for d in string.ascii_uppercase
                 if os.path.isdir(f"{d}:\\")]
    else:
        roots = ["/"]
        home = str(Path.home())
        if home not in roots:
            roots.append(home)

    out: list[tuple[str, int, int]] = []
    for r in roots:
        try:
            u = shutil.disk_usage(r)
        except OSError:
            continue                      # empty card reader, disconnected share
        out.append((r, u.free, u.total))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def free_bytes(path: str) -> int | None:
    """Free space on the volume holding `path` (walking up to an existing dir)."""
    p = Path(path).expanduser()
    for cand in [p, *p.parents]:
        if cand.exists():
            try:
                return shutil.disk_usage(str(cand)).free
            except OSError:
                return None
    return None


def _gb(n: float) -> str:
    return f"{n / (1 << 30):.0f} GB"


def benchmark_drive(root: str, size_bytes: int) -> float | None:
    """Raw sustained sequential write speed of `root`, in MiB/s.

    Writes `size_bytes` of random data (not zeros — some SSD firmware
    fast-paths an all-zero write, which would over-report) in 8 MB chunks,
    flushes and fsyncs so the OS write-back cache can't fake the number, then
    deletes the file. **The test file must be large enough to run past the
    drive's own SLC write cache** or a slow drive reads fast — this is why
    the ceiling one session mistook for the writer/GIL (PLAN.md sec 6 item 1)
    turned out to be a SATA drive: a short burst would have hidden it.

    Returns None if the drive refuses the write (permission, disconnected,
    not enough room) — the caller decides how to report that.
    """
    chunk = os.urandom(8 << 20)
    n = max(1, size_bytes // len(chunk))
    path = Path(root) / ".acqapp_drive_speedtest.tmp"
    try:
        t0 = time.perf_counter()
        with open(path, "wb") as f:
            for _ in range(n):
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        elapsed = time.perf_counter() - t0
    except OSError:
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    written_mb = n * len(chunk) / (1 << 20)
    return written_mb / elapsed if elapsed > 0 else None


@dataclass
class SaveConfig:
    """Where a recording goes and what it is named."""
    folder:    str  = ""       # blank -> default_folder()
    subject:   str  = ""
    session:   str  = ""
    template:  str  = "{subject}_{date}_{time}"
    subfolder: bool = True     # give each recording its own directory

    def resolved_folder(self) -> Path:
        return Path(self.folder).expanduser() if self.folder.strip() \
            else default_folder()

    def stem(self, when: datetime | None = None) -> str:
        """Filename stem with tokens substituted (no extension)."""
        when = when or datetime.now()
        out = self.template or "{subject}_{date}_{time}"
        for tok, val in (
            ("{subject}", sanitize(self.subject, "subject")),
            ("{session}", sanitize(self.session, "")),
            ("{date}",    when.strftime("%Y%m%d")),
            ("{time}",    when.strftime("%H%M%S")),
        ):
            out = out.replace(tok, val)
        out = re.sub(r"_{2,}", "_", out).strip("_ ")     # tidy empty tokens
        return sanitize(out, when.strftime("session_%Y%m%d_%H%M%S"))

    def _path_for(self, base: Path, stem: str) -> Path:
        return (base / stem / f"{stem}.h5") if self.subfolder \
            else (base / f"{stem}.h5")

    def resolve(self, when: datetime | None = None, *,
                unique: bool = False) -> Path:
        """Full path of the .h5 file for a recording starting now.

        With `unique=True` the returned path does not exist: `_001`, `_002`, …
        until the name is free. A template without `{time}` resolves every
        recording of the day to one path; the writer would refuse that (mode
        "x"), so this is not about truncation — auto-numbering keeps the Record
        button working with an animal on the rig.
        """
        stem = self.stem(when)
        base = self.resolved_folder()
        path = self._path_for(base, stem)
        if not unique:
            return path
        for n in range(1, 1000):
            if not path.exists():
                return path
            path = self._path_for(base, f"{stem}_{n:03d}")
        # 999 collisions means the template is degenerate. Fall back to a stem
        # that cannot collide rather than handing back an occupied path.
        when = when or datetime.now()
        return self._path_for(base, f"{stem}_{when.strftime('%H%M%S_%f')}")


def default_folder() -> Path:
    """Largest-free-space fixed drive, so the default is not the system drive."""
    drives = list_drives()
    if drives:
        return Path(drives[0][0]) / _DEFAULT_SUBDIR
    return Path.cwd() / "sessions"


