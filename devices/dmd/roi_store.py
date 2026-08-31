"""Save/load for named ROI sets (`roi.py`'s `RoiSet`). No Qt — the picker is
`roi_picker.py`.

Two folders under `rois/`: `session/` holds sets saved during THIS run of
acqApp, for the quick list in the editor's Load dialog; `archive/` holds
every earlier run's sets, still loadable but reached only through Browse.
Rotation is per PROCESS, not per "close the app" — there is no reliable hook
for the latter (Task Manager, a crash), so whatever `session/` holds is moved
into `archive/` once, the first time this module is touched in a run.
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
            dest = ARCHIVE_DIR / f"{p.name[:-len('.roi.json')]}_{datetime.now():%Y%m%d_%H%M%S}.roi.json"
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
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RoiSet.from_list(data.get("rois", []))


def load_named(path: str | Path) -> tuple[str, RoiSet]:
    """Like `load`, plus the name it was actually SAVED under.

    Not the filename stem: `save()` sanitizes and de-duplicates the stem
    (spaces/punctuation stripped, `_2` on a collision), so a set named
    "column A" can live in "column_A_2.roi.json" — stripping the suffix off
    the path would show the mangled stem, not what the operator typed.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return (data.get("name", Path(path).stem), RoiSet.from_list(data.get("rois", [])))


def list_session() -> list[SavedRoiSet]:
    _rotate_once()
    return _list(SESSION_DIR)


def list_archive() -> list[SavedRoiSet]:
    _rotate_once()
    return _list(ARCHIVE_DIR)


def _list(folder: Path) -> list[SavedRoiSet]:
    out = []
    for p in sorted(folder.glob("*.roi.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(SavedRoiSet(p, data.get("name", p.stem), data.get("saved_at", "")))
    return out


def is_roi_file(path: str | Path) -> bool:
    return Path(path).name.endswith(".roi.json")
