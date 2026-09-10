"""A small dialog for choosing a saved FOV bookmark: `fov_store`'s Qt front
end.

Shared by the Stage tab's "Go to FOV…" button and a routine step's "FOV…"
button — both need the same "this session's FOVs first, older ones through
Browse" shape as the DMD's `RoiSetPicker` (`devices/dmd/roi_picker.py`), plus
a thumbnail per row so a saved FOV is recognized by eye, not just by name.
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout)

from acqApp.devices.stage import fov_store

_PATH_ROLE = Qt.ItemDataRole.UserRole
_THUMB = QSize(64, 64)


class FovPicker(QDialog):
    """Modal picker. `.fov` is the chosen `SavedFov` after `exec()`, else `None`."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a saved FOV")
        self.fov: fov_store.SavedFov | None = None

        v = QVBoxLayout(self)
        v.addWidget(QLabel("This session:"))
        self._list = QListWidget()
        self._list.setIconSize(_THUMB)
        fovs = list(reversed(fov_store.list_session()))       # newest first
        for f in fovs:
            z_txt = "" if f.z_um is None else f", {f.z_um:.0f}"
            it = QListWidgetItem(
                f"{f.name}  ({f.saved_at})  [{f.x_um:.0f}, {f.y_um:.0f}{z_txt}] µm")
            it.setData(_PATH_ROLE, str(f.path))
            if f.image_path is not None:
                pix = QPixmap(str(f.image_path))
                if not pix.isNull():
                    it.setIcon(QIcon(pix))
            self._list.addItem(it)
        if fovs:
            self._list.setCurrentRow(0)
        else:
            self._list.addItem("(none saved this session)")
            self._list.setEnabled(False)
        self._list.itemDoubleClicked.connect(lambda _it: self._accept_selected())
        v.addWidget(self._list)

        row = QHBoxLayout()
        btn_browse = QPushButton("Browse older…")
        btn_browse.setToolTip(
            "FOVs from earlier runs of the app live here, not in the quick "
            "list above, so a long history never slows finding today's spot.")
        btn_browse.clicked.connect(self._browse)
        row.addWidget(btn_browse)
        row.addStretch(1)
        v.addLayout(row)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                               QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept_selected)
        box.rejected.connect(self.reject)
        v.addWidget(box)

    def _accept_selected(self) -> None:
        if not self._list.isEnabled():
            return
        items = self._list.selectedItems()
        if not items:
            return
        self.fov = fov_store.load(items[0].data(_PATH_ROLE))
        self.accept()

    def _browse(self) -> None:
        fov_store.list_archive()          # ensures the folder (+ rotation) exist
        path, _ = QFileDialog.getOpenFileName(
            self, "Older FOV", str(fov_store.ARCHIVE_DIR),
            "FOV bookmarks (*.fov.json);;All files (*)")
        if path:
            self.fov = fov_store.load(path)
            self.accept()
