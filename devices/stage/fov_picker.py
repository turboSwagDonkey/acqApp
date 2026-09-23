"""A small dialog for choosing a saved FOV bookmark: `fov_store`'s Qt front
end.

Shared by the Stage tab's "Go to FOV…" button and a routine step's "FOV…"
button. Same `widgets.SessionPicker` shape as the DMD's `RoiSetPicker`
(`devices/dmd/roi_picker.py`), plus the two things a FOV wants that an ROI
set doesn't: its position in the row, and a thumbnail so a saved spot is
recognized by eye rather than by name.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QIcon, QPixmap

from acqApp.devices.stage import fov_store
from acqApp.widgets import SessionPicker

_THUMB = QSize(64, 64)


class FovPicker(SessionPicker):
    """Modal picker. `.fov` is the chosen `SavedFov` after `exec()`, else `None`."""

    def __init__(self, parent=None):
        super().__init__(
            parent, fov_store,
            title="Choose a saved FOV",
            empty="(none saved this session)",
            browse_caption="Older FOV",
            browse_filter="FOV bookmarks (*.fov.json);;All files (*)",
            browse_tip="FOVs from earlier runs live here, not in the quick "
                       "list above, so a long history never slows finding "
                       "today's spot.",
            icon_size=_THUMB)
        self.fov: fov_store.SavedFov | None = None

    def row(self, rec) -> tuple[str, QIcon | None]:
        z_txt = "" if rec.z_um is None else f", {rec.z_um:.0f}"
        text = (f"{rec.name}  ({rec.saved_at})  "
                f"[{rec.x_um:.0f}, {rec.y_um:.0f}{z_txt}] µm")
        if rec.image_path is None:
            return text, None
        pix = QPixmap(str(rec.image_path))
        return text, (None if pix.isNull() else QIcon(pix))

    def chose(self, path: Path) -> None:
        # Callers read `.fov`, not `.path` — the position is the point.
        self.fov = fov_store.load(path)
        super().chose(path)
