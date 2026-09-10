"""Save/load for named FOV (field-of-view) bookmarks: a stage position plus a
camera snapshot, so the operator can recognize a saved spot later by eye. No
Qt — the picker is `fov_picker.py`.

Modeled directly on `devices/dmd/roi_store.py`: the same `session/`/`archive/`
split and per-process rotation (see that file's docstring for why). New here:
each save also writes a sibling PNG thumbnail, moved alongside its JSON by
the same rotation step.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

_ROOT = Path(__file__).resolve().parents[2] / "fov_library"
SESSION_DIR = _ROOT / "session"
ARCHIVE_DIR = _ROOT / "archive"

SUFFIX = ".fov.json"
IMG_SUFFIX = ".fov.png"

_rotated = False


def _rotate_once() -> None:
    global _rotated
    if _rotated:
        return
    _rotated = True
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for p in SESSION_DIR.glob(f"*{SUFFIX}"):
        dest = ARCHIVE_DIR / p.name
        if dest.exists():           # same name from an earlier run — keep both
            stem = p.name[:-len(SUFFIX)]
            dest = ARCHIVE_DIR / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}{SUFFIX}"
        img = p.with_name(p.name[:-len(SUFFIX)] + IMG_SUFFIX)
        dest_img = dest.with_name(dest.name[:-len(SUFFIX)] + IMG_SUFFIX)
        shutil.move(str(p), str(dest))
        if img.exists():
            shutil.move(str(img), str(dest_img))


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
    """Write a named FOV (+ optional PNG snapshot) into the session folder,
    -> the JSON path used."""
    _rotate_once()
    stem = "".join(c if c.isalnum() or c in "-_ " else "_"
                   for c in name).strip() or "fov"
    path = SESSION_DIR / f"{stem}{SUFFIX}"
    n = 1
    while path.exists():
        n += 1
        path = SESSION_DIR / f"{stem}_{n}{SUFFIX}"
    if png_bytes is not None:
        img_path = path.with_name(path.name[:-len(SUFFIX)] + IMG_SUFFIX)
        img_path.write_bytes(png_bytes)
    payload = {"name": name,
              "saved_at": datetime.now().isoformat(timespec="seconds"),
              "x_um": x_um, "y_um": y_um, "z_um": z_um,
              "camera_preset": camera_preset}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> SavedFov:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    img = path.with_name(path.name[:-len(SUFFIX)] + IMG_SUFFIX)
    z = data.get("z_um")
    return SavedFov(
        path=path,
        name=data.get("name", path.stem),
        saved_at=data.get("saved_at", ""),
        x_um=float(data.get("x_um", 0.0)),
        y_um=float(data.get("y_um", 0.0)),
        z_um=None if z is None else float(z),
        camera_preset=data.get("camera_preset"),
        image_path=img if img.is_file() else None)


def list_session() -> list[SavedFov]:
    _rotate_once()
    return _list(SESSION_DIR)


def list_archive() -> list[SavedFov]:
    _rotate_once()
    return _list(ARCHIVE_DIR)


def _list(folder: Path) -> list[SavedFov]:
    out = []
    for p in sorted(folder.glob(f"*{SUFFIX}")):
        try:
            out.append(load(p))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def is_fov_file(path: str | Path) -> bool:
    return Path(path).name.endswith(SUFFIX)
