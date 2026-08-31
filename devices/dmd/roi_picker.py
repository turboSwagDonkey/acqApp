"""A small dialog for choosing a saved ROI set: `roi_store`'s Qt front end.

Shared by the ROI editor's Load button and a routine step's Pattern picker —
both need the same "this session's sets first, older ones through Browse"
shape, so it lives once rather than being copied into each caller.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout)

from acqApp.devices.dmd import roi_store

_PATH_ROLE = Qt.ItemDataRole.UserRole


class RoiSetPicker(QDialog):
    """Modal picker. `.path` is the chosen file after `exec()`, else `None`."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose an ROI set")
        self.path: Path | None = None

        v = QVBoxLayout(self)
        v.addWidget(QLabel("This session:"))
        self._list = QListWidget()
        sets = list(reversed(roi_store.list_session()))       # newest first
        for s in sets:
            it = QListWidgetItem(f"{s.name}  ({s.saved_at})")
            it.setData(_PATH_ROLE, str(s.path))
            self._list.addItem(it)
        if sets:
            self._list.setCurrentRow(0)
        else:
            self._list.addItem("(none saved this session)")
            self._list.setEnabled(False)
        self._list.itemDoubleClicked.connect(lambda _it: self._accept_selected())
        v.addWidget(self._list)

        row = QHBoxLayout()
        btn_browse = QPushButton("Browse older…")
        btn_browse.setToolTip(
            "Sets from earlier runs of the app live here, not in the quick "
            "list above, so a long history never slows finding today's set.")
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
        self.path = Path(items[0].data(_PATH_ROLE))
        self.accept()

    def _browse(self) -> None:
        roi_store.list_archive()          # ensures the folder (+ rotation) exist
        path, _ = QFileDialog.getOpenFileName(
            self, "Older ROI set", str(roi_store.ARCHIVE_DIR),
            "ROI sets (*.roi.json);;All files (*)")
        if path:
            self.path = Path(path)
            self.accept()
