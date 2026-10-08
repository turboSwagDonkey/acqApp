"""Save/load for named ROI sets. No Qt; the picker is `roi_picker.py`.

`rois/session/` holds this run's sets (the quick list); `rois/archive/` holds
earlier runs' (Browse only). Rotation is per PROCESS — app close has no
reliable hook (Task Manager, a crash) — so `session/` is archived once, the
first time this module is touched in a run.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from acqApp.devices.dmd.roi import RoiSet

_ROOT = Path(__file__).resolve().parents[2] / "rois"
SESSION_DIR = _ROOT / "session"
ARCHIVE_DIR = _ROOT / "archive"

_rotated = False


def _rotate_once() -> None:
    global _rotated
    if _rotated:
        return
    _rotated = True
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for p in SESSION_DIR.glob("*.roi.json"):
        dest = ARCHIVE_DIR / p.name
        if dest.exists():           # same name from an earlier run — keep both
            stem = p.name[:-len(".roi.json")]
            dest = ARCHIVE_DIR / (f"{stem}_{datetime.now():%Y%m%d_%H%M%S}"
                                  ".roi.json")
        shutil.move(str(p), str(dest))


class SavedRoiSet(NamedTuple):
    path: Path
    name: str
    saved_at: str


def save(name: str, rois: RoiSet) -> Path:
    """Write `rois` into the session folder under `name` -> the path used."""
    _rotate_once()
    stem = "".join(c if c.isalnum() or c in "-_ " else "_"
                   for c in name).strip() or "roi"
    path = SESSION_DIR / f"{stem}.roi.json"
    n = 1
    while path.exists():
        n += 1
        path = SESSION_DIR / f"{stem}_{n}.roi.json"
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
    return (data.get("name", Path(path).stem),
            RoiSet.from_list(data.get("rois", [])))


def list_session() -> list[SavedRoiSet]:
    _rotate_once()
    return list_folder(SESSION_DIR)


def list_archive() -> list[SavedRoiSet]:
    _rotate_once()
    return list_folder(ARCHIVE_DIR)


def list_folder(folder: Path) -> list[SavedRoiSet]:
    out = []
    for p in sorted(folder.glob("*.roi.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(SavedRoiSet(p, data.get("name", p.stem),
                               data.get("saved_at", "")))
    return out


def is_roi_file(path: str | Path) -> bool:
    return Path(path).name.endswith(".roi.json")
