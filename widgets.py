"""Shared panel widgets.

The settings panels are per-instrument and own their own layout; this is the
little all of them want. Kept Qt-only and knowing nothing about devices, so a
panel can use it without pulling in the shell.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QGroupBox, QWidget

_GEOM_ORG, _GEOM_APP = "acqApp", "acqApp"


def collapsible(box: QGroupBox, expanded: bool = True) -> QGroupBox:
    """Fold `box`'s contents away behind the tick in its title.

    Hiding the direct children is the whole implementation — the box shrinks to
    its title on its own (measured: 141 px → 43), so there is no height to
    juggle. Qt also disables them while unticked, and restores each child's
    *own* enabled state on the way back, so a control the panel had deliberately
    greyed out is still greyed out afterwards.
    """
    kids = [c for c in box.children() if isinstance(c, QWidget)]

    def _show(on: bool) -> None:
        for w in kids:
            w.setVisible(on)

    box.setCheckable(True)
    box.setChecked(expanded)
    _show(expanded)
    box.toggled.connect(_show)
    if not box.toolTip():
        box.setToolTip("Untick the title to fold this section away.")
    return box


def collapsible_groups(panel: QWidget, key: str) -> list[QGroupBox]:
    """Make every group box in `panel` collapsible, and remember which are shut.

    Applied centrally rather than in each panel, so a new instrument gets it
    without doing anything — and so the panels stay about their instrument.

    **Boxes that are already checkable are skipped.** A tick there means
    something to the panel already (the pupil tab's "Advanced tracking" folds
    itself and renames its own title), and taking it over would fight it.
    """
    s = QSettings(_GEOM_ORG, _GEOM_APP)
    done: list[QGroupBox] = []
    for box in panel.findChildren(QGroupBox):
        if box.isCheckable():
            continue
        # Keyed by title, not position: inserting a group above should not
        # shuffle everyone's saved state. Titles of skipped boxes can be
        # dynamic, which is another reason to leave those alone.
        setting = f"collapse/{key}/{box.title()}"
        collapsible(box, s.value(setting, "1") not in (False, "false", "0", 0))
        box.toggled.connect(
            lambda on, k=setting: QSettings(_GEOM_ORG, _GEOM_APP).setValue(
                k, "1" if on else "0"))
        done.append(box)
    return done
