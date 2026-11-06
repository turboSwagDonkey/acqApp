"""Look back at a saved FOV: the image kept when its DMD ROIs were saved
(snapshot + shapes), else the plain FOV snapshot. Read-only; the adapter
picks the image, so this never knows about the DMD."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QSizePolicy,
                             QVBoxLayout)


class FovPreview(QDialog):
    """Non-modal: it can stay open beside the live view."""

    def __init__(self, title: str, caption: str, image: Path | None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 800)
        self._pix = QPixmap(str(image)) if image is not None else QPixmap()

        root = QVBoxLayout(self)
        cap = QLabel(caption)
        cap.setWordWrap(True)
        root.addWidget(cap)
        self._img = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        # Ignored: the pixmap is rescaled to the label, never the reverse.
        self._img.setSizePolicy(QSizePolicy.Policy.Ignored,
                                QSizePolicy.Policy.Ignored)
        self._img.setMinimumSize(200, 200)
        if self._pix.isNull():
            self._img.setText("No snapshot was captured for this FOV "
                              "(no voltage-cam frame when it was saved).")
        root.addWidget(self._img, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    @property
    def has_image(self) -> bool:
        return not self._pix.isNull()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if not self._pix.isNull():
            self._img.setPixmap(self._pix.scaled(
                self._img.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
