"""The file side `devices/dmd/roi_store.py` and `devices/stage/fov_store.py`
share. No Qt.

`session/` holds this run's files (the quick list); `archive/` holds earlier
runs' (Browse only). Rotation is per PROCESS — app close has no reliable
hook (Task Manager, a crash) — so each store archives `session/` once, the
first time it is touched in a run. A file's sibling PNG moves with it.
Folders are passed in, not held: the tests repoint each store's.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


def sibling(path: Path, suffix: str, img_suffix: str) -> Path:
    """"x<suffix>" -> "x<img_suffix>" beside it (it may not exist)."""
    return path.with_name(path.name[:-len(suffix)] + img_suffix)


def rotate(session: Path, archive: Path, suffix: str, img_suffix: str) -> None:
    """Move every `*<suffix>` (and its PNG) from `session` into `archive`."""
    session.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    for p in session.glob(f"*{suffix}"):
        dest = archive / p.name
        if dest.exists():           # same name from an earlier run — keep both
            dest = archive / (f"{p.name[:-len(suffix)]}_"
                              f"{datetime.now():%Y%m%d_%H%M%S}{suffix}")
        img = sibling(p, suffix, img_suffix)
        shutil.move(str(p), str(dest))
        if img.exists():
            shutil.move(str(img), str(sibling(dest, suffix, img_suffix)))


def fresh_path(folder: Path, name: str, suffix: str, default: str) -> Path:
    """`name` made filename-safe, de-duplicated with _2, _3, ... in `folder`."""
    stem = "".join(c if c.isalnum() or c in "-_ " else "_"
                   for c in name).strip() or default
    path, n = folder / f"{stem}{suffix}", 1
    while path.exists():
        n += 1
        path = folder / f"{stem}_{n}{suffix}"
    return path
