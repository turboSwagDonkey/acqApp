"""The window's layout: General column, one dock per own-dock module,
the status bar, and saving/restoring the dock layout."""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, QSettings
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QComboBox, QDockWidget, QFrame, QLabel, QHBoxLayout, QMainWindow,
    QPushButton, QScrollArea, QSplitter, QStatusBar, QToolButton, QVBoxLayout,
    QWidget,
)

from acqApp import config, style
from acqApp.saving import SaveConfig, SavePanel
from acqApp.sidebar import Fold, MiniText
from acqApp.widgets import align_grids, compact
from acqApp.window.common import MODE_NONE, MODULES_TIP, module_rank


# Saved dock layouts of an older version are ignored, not half-applied.
_LAYOUT_VERSION = 4
# An own-dock module's default place, (column, row). General tops column
# _GENERAL_COL; the others lie to its right.
_GENERAL_COL = -1
_SLOTS = {"stage": (_GENERAL_COL, 1),
          "voltage_cam": (0, 0), "routines": (0, 1),
          "dmd": (1, 0), "pupil_cam": (1, 1)}


def _section_rank(key: str) -> int:
    return -1 if key == "saving" else module_rank(key)


def _slot(key: str) -> tuple[int, int]:
    return _SLOTS.get(key, (1, 99))


def _dress(panel: QWidget, key: str) -> None:
    """What every settings panel gets: its accent and aligned grids. Its
    boxes don't fold: the card does, so one click shows them all."""
    panel.setStyleSheet(style.accent_panel(key))
    align_grids(panel)


class _VScroll(QScrollArea):
    """Scrolls only up and down: never narrower than what it holds, so a
    column can't be dragged so thin that its sections need a sideways bar."""

    def __init__(self, w: QWidget) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(w)
        w.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()       # a section opened or shut
        return super().eventFilter(obj, event)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        hint.setWidth(self.widget().minimumSizeHint().width() + bar)
        return hint


class DockLayout:
    """MainWindow's UI build and dock arrangement."""

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """One window, all docks (no central widget, so every pane drags):
        General over stage | voltage cam over routines | DMD over pupil cam."""
        self.resize(1600, 900)
        # Nested, never tabbed: a tab would hide one region behind another
        # (General behind the stage, say).
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks
                            | QMainWindow.DockOption.AllowNestedDocks)

        self._build_general()
        # Every panel before any view: controllers read them.
        for m in self._modules:
            m.build_panel()
        for m in self._modules:
            self._place_module(m)

        self._build_status_bar()
        self._arrange_docks()
        self._restore_layout()

    def _build_general(self) -> None:
        """The left dock: Save and every module without a dock of its own,
        each folded under its card, over the session-wide controls."""
        save_cfg = config.load_dataclass(SaveConfig, "saving")
        if not save_cfg.mouse_id:
            # Legacy key: `subject` became mouse_id on 2026-09-14.
            old_subject = config.load_settings("saving").get("subject")
            if isinstance(old_subject, str) and old_subject.strip():
                save_cfg.mouse_id = old_subject.strip()
        self._save_panel = SavePanel(save_cfg)
        self._save_panel.settings_changed.connect(self._save_save_settings)
        _dress(self._save_panel, "saving")

        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(4, 4, 4, 4)
        self._sections = QVBoxLayout()
        self._sections.setSpacing(6)
        col.addLayout(self._sections)
        col.addStretch(1)
        self._add_section("saving", "Save", self._mini("saving"), self._save_panel)

        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        self._general_scroll = _VScroll(inner)
        lay.addWidget(self._general_scroll, 1)
        lay.addWidget(self._build_controls())
        self._general_dock = self._make_dock(
            "General", host, Qt.DockWidgetArea.LeftDockWidgetArea)

    def _build_controls(self) -> QWidget:
        """One line: Mode, then small buttons (icons, bar Modules)."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)

        self._modes = config.load_modes()
        self._mode_combo = compact(QComboBox())
        self._mode_combo.addItems((MODE_NONE, *self._modes))
        self._mode_combo.setToolTip(
            "Apply a named preset across modules (DMD illumination, camera "
            "capture area, ...), defined in modes.json. \"None\" leaves "
            "everything as set.")
        self._mode_combo.currentTextChanged.connect(self.set_mode)
        row.addWidget(QLabel("Mode:"))
        row.addWidget(self._mode_combo)

        self._save_mode_action = QAction("💾", self)
        self._save_mode_action.setToolTip(
            "Save as preset: capture the DMD's and voltage camera's CURRENT "
            "settings as a new (or replacement) entry in modes.json.")
        self._save_mode_action.triggered.connect(self._save_mode_as)

        self._modules_action = QAction("🧩 Modules", self)
        self._modules_action.setToolTip(MODULES_TIP)
        self._modules_action.triggered.connect(self._open_modules_dialog)

        self._reset_action = QAction("⟲", self)
        self._reset_action.setToolTip(
            "Reset layout: put every pane back where it started (drag a pane "
            "by its title bar to move it; drag the gaps between panes to "
            "resize)")
        self._reset_action.triggered.connect(self._arrange_docks)

        row.addStretch()
        for act in (self._save_mode_action, self._modules_action,
                    self._reset_action):
            b = QToolButton()
            b.setDefaultAction(act)
            row.addWidget(b)
        return box

    def _mini(self, key: str) -> QWidget | None:
        """A fresh live view for `key`'s card."""
        if key == "saving":
            return MiniText(self._save_mini_text)
        m = self._module(key)
        return m.mini_view() if m is not None else None

    def _place_module(self, m) -> None:
        """Its settings folded under its card: in its own dock over its
        view, else as a section of General."""
        if m.panel is not None:
            _dress(m.panel, m.key)
        mini = m.mini_view()
        if not m.own_dock:
            if m.panel is not None or mini is not None:
                self._add_section(m.key, m.tab_label, mini, m.panel)
            return
        view = m.view_widget()
        body = _VScroll(m.panel) if m.panel is not None else None
        fold = Fold(m.key, "Settings", mini, body, default_open=view is None)
        self._folds[m.key] = fold
        self._docks[m.key] = self._make_dock(
            m.tab_label, self._dock_body(fold, view),
            Qt.DockWidgetArea.LeftDockWidgetArea, accent=m.key)

    def _add_section(self, key: str, label: str, mini: QWidget | None,
                     panel: QWidget | None) -> None:
        fold = Fold(key, label, mini, panel)
        at = sum(1 for k in self._folds
                 if k not in self._docks and _section_rank(k) < _section_rank(key))
        self._sections.insertWidget(at, fold)
        self._folds[key] = fold

    @staticmethod
    def _dock_body(fold: Fold, view: QWidget | None) -> QWidget:
        """The view over the fold (its Settings at the bottom), the line
        between them draggable; with no view, the fold fills the dock."""
        if view is None:
            host = QWidget()
            lay = QVBoxLayout(host)
            lay.setContentsMargins(4, 4, 4, 4)
            lay.addWidget(fold, 1)
            lay.addStretch(0)
            return host
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(view)
        split.addWidget(fold)
        split.setStretchFactor(0, 1)

        def opened(on: bool) -> None:
            total = sum(split.sizes())
            if total:       # shut, the card alone: no band left over it
                want = (min(fold.sizeHint().height(), total * 3 // 5) if on
                        else fold.maximumHeight())
                split.setSizes([total - want, want])
        fold.toggled.connect(opened)
        return split

    def _build_status_bar(self) -> None:
        self._btn_emulate = QPushButton("Emulate")
        self._btn_emulate.setCheckable(True)
        self._btn_emulate.setChecked(self._emulate)
        self._btn_emulate.setStyleSheet(style.quiet_toggle("wheel"))
        self._btn_emulate.setToolTip("Use simulated signals instead of hardware")
        self._btn_emulate.toggled.connect(self._on_emulate_toggled)
        self._btn_emulate.toggled.connect(self._label_emulate)
        self._label_emulate(self._emulate)

        self._btn_run = QPushButton("Live view")
        self._btn_run.setCheckable(True)
        self._btn_run.setStyleSheet(style.toggle_btn("sync"))
        self._btn_run.setToolTip("Show live signals from all devices (not saved)")
        self._btn_run.toggled.connect(self._on_run_toggled)

        self._btn_rec = QPushButton("● Record")
        self._btn_rec.setCheckable(True)
        self._btn_rec.setStyleSheet(style.record_btn("puffer"))
        self._btn_rec.setToolTip("Live view and save every stream to disk")
        self._btn_rec.toggled.connect(self._on_record_toggled)

        # Permanent labels, not status(): any status() call would wipe them.
        self._lbl_rec = QLabel("")
        self._lbl_rec.setStyleSheet("color:#9aa0a6;")
        self._lbl_time = QLabel("")
        self._lbl_time.setStyleSheet("color:#9aa0a6;")

        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.addPermanentWidget(self._lbl_time)
        sb.addPermanentWidget(self._lbl_rec)
        sb.addPermanentWidget(self._btn_emulate)
        sb.addPermanentWidget(self._btn_run)
        sb.addPermanentWidget(self._btn_rec)
        sb.showMessage("Ready")
        for m in self._modules:
            self._add_status_widget(m)
        self._size_status_boxes()

    def _add_status_widget(self, m) -> None:
        """Place the control a module offers (the DMD's light switch) beside Record."""
        w = m.status_widget()
        if w is not None:
            self.statusBar().addPermanentWidget(w)
            w.show()
            self._status_widgets[m.key] = w

    def _size_status_boxes(self) -> None:
        """Record and each module switch get one fixed box, wide enough for
        every label any of them shows: no resizing between states, no clipping."""
        rec, keep = self._btn_rec, self._btn_rec.text()
        w = h = 0
        for text in ("● Record", "■ Stop rec"):
            rec.setText(text)
            hint = rec.sizeHint()
            w, h = max(w, hint.width()), max(h, hint.height())
        rec.setText(keep)
        # Buttons only: a switch (the mirror's) keeps its own shape.
        buttons = [b for b in self._status_widgets.values()
                   if isinstance(b, QPushButton)]
        for b in buttons:
            w, h = max(w, b.minimumWidth(), b.sizeHint().width()), max(h, b.sizeHint().height())
        for b in (rec, *buttons):
            b.setFixedSize(w, h)

    def _remove_status_widget(self, key: str) -> None:
        w = self._status_widgets.pop(key, None)
        if w is not None:
            self.statusBar().removeWidget(w)
            w.setParent(None)
            w.deleteLater()

    def _make_dock(self, title: str, widget: QWidget,
                   area: "Qt.DockWidgetArea", accent: str = "sync") -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title}")
        zone = QFrame()                 # the accent frame under the title band
        zone.setObjectName("zone")
        zone.setStyleSheet(style.zone(accent))
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.addWidget(widget)
        dock.setWidget(zone)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        # Rearranged within the window, never torn out of it or closed.
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        dock.setStyleSheet(style.dock_accent(accent))
        self.addDockWidget(area, dock)
        return dock

    def _save_mini_text(self) -> tuple[str, str]:
        path = self.recording_path()
        if path is not None:
            return "● REC", path.name
        s = self._save_panel.settings
        return "", f"{s.mouse_id or 'no mouse ID'} → {s.resolved_folder().name}"

    # ── Dock layout ─────────────────────────────────────────────────────────────

    def _label_emulate(self, on: bool) -> None:
        self._btn_emulate.setText("EMULATE: no hardware" if on else "Emulate")

    def _arrange_docks(self) -> None:
        """The default layout, also Reset layout: a column per `_SLOTS`
        column, its docks stacked by row, General on top of the first."""
        general = self._general_dock
        for d in (general, *self._docks.values()):
            d.setFloating(False)
            self.removeDockWidget(d)
        cols: dict[int, list] = {_GENERAL_COL: [general]}
        for key in sorted(self._docks, key=lambda k: (_slot(k), module_rank(k))):
            cols.setdefault(_slot(key)[0], []).append(self._docks[key])
        stacks = [cols[c] for c in sorted(cols)]
        # Columns first, then each split into rows: a row split first would
        # put the next column beside one row only.
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, general)
        for prev, stack in zip(stacks, stacks[1:]):
            self.splitDockWidget(prev[0], stack[0], Qt.Orientation.Horizontal)
        for stack in stacks:
            for upper, lower in zip(stack, stack[1:]):
                self.splitDockWidget(upper, lower, Qt.Orientation.Vertical)
        for d in (general, *self._docks.values()):
            d.show()
        widths = [370, 750, 480][:len(stacks)]  # narrow left: the stage map fills it
        self.resizeDocks([s[0] for s in stacks], widths,
                         Qt.Orientation.Horizontal)
        for stack in stacks:
            if len(stack) == 2:
                # General takes only its cards' height: the stage map gets the rest.
                # resizeDocks treats sizes as ratios, so these must add up.
                h = self._general_height()
                self.resizeDocks(stack, ([h, max(1, self.height() - h)]
                                         if stack[0] is general else [3, 2]),
                                 Qt.Orientation.Vertical)

    def _general_height(self) -> int:
        """The dock's hint with the scroll area's stock ~24 lines swapped for
        what it holds."""
        scroll = self._general_scroll
        return (self._general_dock.sizeHint().height()
                - scroll.sizeHint().height()
                + scroll.widget().sizeHint().height())

    def _dock_beside(self, key: str) -> None:
        """A dock loaded mid-session: under a docked neighbour in its column
        (General heads its own), else right of General."""
        dock = self._docks.get(key)
        if dock is None:
            return
        col = _slot(key)[0]
        sib = next((d for k, d in self._docks.items()
                    if k != key and _slot(k)[0] == col and not d.isFloating()),
                   self._general_dock if col == _GENERAL_COL else None)
        if sib is not None:
            self.splitDockWidget(sib, dock, Qt.Orientation.Vertical)
        else:
            self.splitDockWidget(self._general_dock, dock,
                                 Qt.Orientation.Horizontal)

    def _restore_layout(self) -> None:
        state = QSettings("acqApp", "acqApp").value("dockState")
        if state is not None:
            self.restoreState(state, _LAYOUT_VERSION)
        # A layout saved while floating was allowed would restore a dock outside.
        for d in (self._general_dock, *self._docks.values()):
            d.setFloating(False)

    def _save_layout(self) -> None:
        QSettings("acqApp", "acqApp").setValue(
            "dockState", self.saveState(_LAYOUT_VERSION))
