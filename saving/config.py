"""Where a session file goes. No Qt."""
from __future__ import annotations

import json
import os
import re
import shutil
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')   # Windows-invalid + control chars
_DEFAULT_SUBDIR = "acq_sessions"

TOKENS = ("{mouse_id}", "{project}", "{date}", "{time}")


def sanitize(name: str, fallback: str = "session") -> str:
    """Safe as a single path component."""
    cleaned = _BAD.sub("_", (name or "").strip()).strip(" .")
    return cleaned or fallback


def list_drives() -> list[tuple[str, int, int]]:
    """[(root, free, total)] for readable drives, most free first."""
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
    """Free space on the volume holding `path` (or its nearest existing parent)."""
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
    """Sustained sequential write speed of `root`, MiB/s; None if refused.

    Random data (some firmware fast-paths zeros), fsynced, and large enough to
    run past the drive's SLC cache — a short burst once hid a SATA drive's
    real ceiling."""
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


DEFAULT_TEMPLATE = "{mouse_id}_{date}_{time}"


@dataclass
class SaveConfig:
    folder:    str  = ""       # blank -> default_folder()
    mouse_id:  str  = ""
    project:   str  = ""
    template:  str  = DEFAULT_TEMPLATE
    subfolder: bool = True     # each recording in its own directory
    append_fov: bool = False   # suffix the active FOV's name
    # Split mode: TIFF/DCIMG per image stream, one CSV, one JSON — a folder
    # instead of one .h5.
    split:       bool = False
    orca_format: str  = "tiff"   # "tiff" or "dcimg"; split mode only

    def resolved_folder(self) -> Path:
        return Path(self.folder).expanduser() if self.folder.strip() \
            else default_folder()

    def stem(self, when: datetime | None = None, *, fov: str = "") -> str:
        """Template with tokens substituted, plus `_<fov>` if given."""
        when = when or datetime.now()
        out = self.template or DEFAULT_TEMPLATE
        for tok, val in (
            ("{mouse_id}", sanitize(self.mouse_id, "mouse_id")),
            ("{project}",  sanitize(self.project, "")),
            ("{date}",     when.strftime("%Y%m%d")),
            ("{time}",     when.strftime("%H%M%S")),
        ):
            out = out.replace(tok, val)
        out = re.sub(r"_{2,}", "_", out).strip("_ ")     # tidy empty tokens
        out = sanitize(out, when.strftime("session_%Y%m%d_%H%M%S"))
        if fov.strip():
            out = f"{out}_{sanitize(fov)}"
        return out

    def _path_for(self, base: Path, stem: str) -> Path:
        return (base / stem / f"{stem}.h5") if self.subfolder \
            else (base / f"{stem}.h5")

    def _dir_for(self, base: Path, stem: str) -> Path:
        return base / stem

    def _resolve_at(self, base: Path, stem: str,
                    build: Callable[[Path, str], Path], *, unique: bool) -> Path:
        """With `unique`, append _001, _002, … until the path is free: the
        writer refuses an existing one, and Record must keep working."""
        path = build(base, stem)
        if not unique:
            return path
        for n in range(1, 1000):
            if not path.exists():
                return path
            path = build(base, f"{stem}_{n:03d}")
        return build(base, f"{stem}_{datetime.now():%H%M%S_%f}")

    def _resolve(self, build: Callable[[Path, str], Path],
                 when: datetime | None, *, unique: bool, fov: str = "") -> Path:
        return self._resolve_at(self.resolved_folder(), self.stem(when, fov=fov),
                                build, unique=unique)

    def resolve(self, when: datetime | None = None, *,
                unique: bool = False, fov: str = "") -> Path:
        """The .h5 for a recording starting now."""
        return self._resolve(self._path_for, when, unique=unique, fov=fov)

    def resolve_dir(self, when: datetime | None = None, *,
                    unique: bool = False, fov: str = "") -> Path:
        """Split mode's session folder (always its own, whatever `subfolder`)."""
        return self._resolve(self._dir_for, when, unique=unique, fov=fov)

    def routine_base(self, when: datetime | None = None) -> Path:
        """<folder>/[<project>/]<mouse_id>/<date>: a routine's fixed hierarchy."""
        when = when or datetime.now()
        base = self.resolved_folder()
        if self.project.strip():
            base /= sanitize(self.project)
        return base / sanitize(self.mouse_id, "mouse_id") / when.strftime("%Y%m%d")

    def resolve_routine(self, fov: str, trial: int,
                        when: datetime | None = None, *,
                        unique: bool = False) -> Path:
        """<routine_base>/FOV<fov>_T<trial>.h5"""
        return self._resolve_at(self.routine_base(when),
                                _routine_stem(fov, trial),
                                self._path_for, unique=unique)

    def resolve_routine_dir(self, fov: str, trial: int,
                            when: datetime | None = None, *,
                            unique: bool = False) -> Path:
        return self._resolve_at(self.routine_base(when),
                                _routine_stem(fov, trial),
                                self._dir_for, unique=unique)


def _routine_stem(fov: str, trial: int) -> str:
    prefix = "" if fov.lower().startswith("fov") else "FOV"
    return sanitize(f"{prefix}{fov}_T{trial}")


def default_folder() -> Path:
    """On the drive with the most free space, not the system drive."""
    drives = list_drives()
    if drives:
        return Path(drives[0][0]) / _DEFAULT_SUBDIR
    return Path.cwd() / "sessions"


def write_routine_fov_sidecar(path: Path, x_um: float | None, y_um: float | None,
                              z_um: float | None) -> None:
    """Raw X/Y/Z for a "FOVcustom" file, beside it (or inside a split folder).
    Only after the recording has created `path`."""
    target = (path / "fov.json") if path.is_dir() \
        else path.with_name(f"{path.stem}.fov.json")
    target.write_text(json.dumps({"x_um": x_um, "y_um": y_um, "z_um": z_um},
                                 indent=2), encoding="utf-8")
