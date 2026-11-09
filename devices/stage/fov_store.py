"""Named FOV bookmarks: a stage position plus an optional camera snapshot
(sibling PNG). No Qt; the picker is `fov_picker.py`.
`fov_library/session/` and `archive/`, rotated as `named_store.py` says.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from acqApp import named_store

_ROOT = Path(__file__).resolve().parents[2] / "fov_library"
SESSION_DIR = _ROOT / "session"
ARCHIVE_DIR = _ROOT / "archive"

SUFFIX = ".fov.json"
IMG_SUFFIX = ".fov.png"

_rotated = False


def _img_for(path: Path) -> Path:
    return named_store.sibling(path, SUFFIX, IMG_SUFFIX)


def _rotate_once() -> None:
    global _rotated
    if not _rotated:
        _rotated = True
        named_store.rotate(SESSION_DIR, ARCHIVE_DIR, SUFFIX, IMG_SUFFIX)


class SavedFov(NamedTuple):
    path:          Path
    name:          str
    saved_at:      str
    x_um:          float
    y_um:          float
    z_um:          float | None
    camera_preset: str | None
    image_path:    Path | None       # None if no snapshot was captured


def save(name: str, x_um: float, y_um: float, z_um: float | None = None,
        camera_preset: str | None = None, png_bytes: bytes | None = None) -> Path:
    """Write a named FOV (+ PNG) into the session folder -> its JSON path."""
    _rotate_once()
    path = named_store.fresh_path(SESSION_DIR, name, SUFFIX, "fov")
    if png_bytes is not None:
        _img_for(path).write_bytes(png_bytes)
    payload = {"name": name,
              "saved_at": datetime.now().isoformat(timespec="seconds"),
              "x_um": x_um, "y_um": y_um, "z_um": z_um,
              "camera_preset": camera_preset}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> SavedFov:
    """Raises OSError or ValueError (TypeError on a non-numeric position)."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: not a saved FOV")
    img = _img_for(path)
    # No default position: a file without one (Browse takes any) must not
    # send the stage to 0,0.
    if "x_um" not in data or "y_um" not in data:
        raise ValueError(f"{path.name}: no saved position")
    x, y, z = float(data["x_um"]), float(data["y_um"]), data.get("z_um")
    z = None if z is None else float(z)
    if not all(math.isfinite(v) for v in (x, y, 0.0 if z is None else z)):
        raise ValueError(f"{path.name}: the position is not a finite number")
    name, saved_at = data.get("name"), data.get("saved_at")
    return SavedFov(
        path=path,
        name=name if isinstance(name, str) else path.stem,
        saved_at=saved_at if isinstance(saved_at, str) else "",
        x_um=x,
        y_um=y,
        z_um=z,
        camera_preset=data.get("camera_preset"),
        image_path=img if img.is_file() else None)


def list_session() -> list[SavedFov]:
    _rotate_once()
    return list_folder(SESSION_DIR)


def list_archive() -> list[SavedFov]:
    _rotate_once()
    return list_folder(ARCHIVE_DIR)


def list_folder(folder: Path) -> list[SavedFov]:
    out = []
    for p in sorted(folder.glob(f"*{SUFFIX}")):
        try:
            out.append(load(p))
        except (OSError, ValueError, TypeError):     # one bad file hides only itself
            continue
    return out
