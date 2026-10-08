"""A saved FOV and its DMD ROI set, paired by name: "<base>_fov" with
"<base>_roi". No Qt.

By the typed name, not the file stem: the stores sanitise and de-duplicate
stems, and archiving renames on collision. Several same-name candidates (an
ROI re-tweaked and saved again) -> the newest in the nearest folder.
"""
from __future__ import annotations

from pathlib import Path

from acqApp.devices.dmd import roi_store
from acqApp.devices.stage import fov_store

FOV_SUFFIX = "_fov"
ROI_SUFFIX = "_roi"


def base_name(name: str) -> str:
    """"cell A_fov" / "cell A_roi" / "cell A" -> "cell A"."""
    name = name.strip()
    for suffix in (FOV_SUFFIX, ROI_SUFFIX):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def fov_name(name: str) -> str:
    return base_name(name) + FOV_SUFFIX


def roi_name(name: str) -> str:
    return base_name(name) + ROI_SUFFIX


def _folders(picked: Path, own, other) -> list[Path]:
    """Where to look in `other`'s store for the partner of a file in `own`'s:
    the matching folder first (archive pairs with archive), then session,
    then archive. A file outside both stores looks beside itself first."""
    near = {own.SESSION_DIR.resolve(): other.SESSION_DIR,
            own.ARCHIVE_DIR.resolve(): other.ARCHIVE_DIR}.get(
                picked.parent.resolve(), picked.parent)
    out: list[Path] = []
    for f in (near, other.SESSION_DIR, other.ARCHIVE_DIR):
        if f.resolve() not in [o.resolve() for o in out] and f.is_dir():
            out.append(f)
    return out


def _newest(hits, path_of):
    """Latest `saved_at` (whole seconds), ties broken by file time."""
    def key(rec):
        try:
            return rec.saved_at, path_of(rec).stat().st_mtime
        except OSError:
            return rec.saved_at, 0.0
    return max(hits, key=key)


def roi_for_fov(fov: fov_store.SavedFov) -> Path | None:
    """The ROI set paired with `fov`, or None (no "_fov" name, or no match)."""
    if not fov.name.strip().lower().endswith(FOV_SUFFIX):
        return None
    want = roi_name(fov.name).lower()
    roi_store.list_session()                    # rotates, once per process
    for folder in _folders(Path(fov.path), fov_store, roi_store):
        hits = [r for r in roi_store.list_folder(folder)
                if r.name.strip().lower() == want]
        if hits:
            return _newest(hits, lambda r: r.path).path
    return None


def fov_for_roi(path: str | Path) -> fov_store.SavedFov | None:
    """The FOV paired with the ROI set at `path`, or None."""
    path = Path(path)
    try:
        name, _ = roi_store.load_named(path)
    except (OSError, ValueError, KeyError):
        return None
    if not name.strip().lower().endswith(ROI_SUFFIX):
        return None
    want = fov_name(name).lower()
    fov_store.list_session()
    for folder in _folders(path, roi_store, fov_store):
        hits = [f for f in fov_store.list_folder(folder)
                if f.name.strip().lower() == want]
        if hits:
            return _newest(hits, lambda f: f.path)
    return None
