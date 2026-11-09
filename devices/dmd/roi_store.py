"""Save/load for named ROI sets. No Qt; the picker is `roi_picker.py`.
`rois/session/` and `rois/archive/`, rotated as `named_store.py` says.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from acqApp import named_store
from acqApp.devices.dmd.roi import RoiSet

_ROOT = Path(__file__).resolve().parents[2] / "rois"
SESSION_DIR = _ROOT / "session"
ARCHIVE_DIR = _ROOT / "archive"

SUFFIX = ".roi.json"
IMG_SUFFIX = ".roi.png"             # the drawing view at Save: snapshot + shapes

_rotated = False


def image_for(path: str | Path) -> Path:
    """Where the set's preview PNG goes (it may not exist)."""
    return named_store.sibling(Path(path), SUFFIX, IMG_SUFFIX)


def _rotate_once() -> None:
    global _rotated
    if not _rotated:
        _rotated = True
        named_store.rotate(SESSION_DIR, ARCHIVE_DIR, SUFFIX, IMG_SUFFIX)


class SavedRoiSet(NamedTuple):
    path: Path
    name: str
    saved_at: str


def save(name: str, rois: RoiSet, png_bytes: bytes | None = None) -> Path:
    """Write `rois` (+ preview PNG) into the session folder under `name`
    -> the path used."""
    _rotate_once()
    path = named_store.fresh_path(SESSION_DIR, name, SUFFIX, "roi")
    if png_bytes is not None:
        image_for(path).write_bytes(png_bytes)
    payload = {"name": name,
              "saved_at": datetime.now().isoformat(timespec="seconds"),
              "rois": rois.to_list()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> RoiSet:
    return load_named(path)[1]


def load_named(path: str | Path) -> tuple[str, RoiSet]:
    """-> (name as typed, set). Not the file stem, which `save()` sanitizes
    and de-duplicates ("L2/3" -> "L2_3_2.roi.json")."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{Path(path).name} is not an ROI set")
    try:
        rois = RoiSet.from_list(data.get("rois", []))
    except (TypeError, AttributeError) as e:   # a malformed entry
        raise ValueError(f"{Path(path).name}: bad ROI ({e})") from e
    return _name(data, Path(path)), rois


def _name(data: dict, path: Path) -> str:
    """The typed name; a hand-edited non-string one falls back to the stem."""
    name = data.get("name")
    return name if isinstance(name, str) else path.stem


def list_session() -> list[SavedRoiSet]:
    _rotate_once()
    return list_folder(SESSION_DIR)


def list_archive() -> list[SavedRoiSet]:
    _rotate_once()
    return list_folder(ARCHIVE_DIR)


def list_folder(folder: Path) -> list[SavedRoiSet]:
    out = []
    for p in sorted(folder.glob(f"*{SUFFIX}")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):   # ValueError: bad JSON or not UTF-8
            continue
        if not isinstance(data, dict):
            continue
        out.append(SavedRoiSet(p, _name(data, p),
                               str(data.get("saved_at", ""))))
    return out


def is_roi_file(path: str | Path) -> bool:
    return Path(path).name.endswith(SUFFIX)
