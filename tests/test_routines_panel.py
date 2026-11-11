"""Experiment routines on screen: the panel, step table, templates, the
whole app, the timeline, and FOV/ROI pairing.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_routines_panel.py [-q] [--part NAME]
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from _harness import (
    Report, isolate_user_state, make_window, pump, qt_app, run_parts,
)
from _routine_rig import FULL_RIG

from acqApp.routines.engine import Phase
from acqApp.routines.settings import (
    KINDS, Group, Recording, RigLimits, Routine, Step, expand, play_order,
    validate,
)
from acqApp.routines.timeline import _group_spans, _layout, _recording_runs


def check_step_repeats(r: Report, app) -> None:
    """The Repeats column: an FOV step's own run count, 5 for a new one."""
    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.settings import play_order
    from acqApp.routines.table import FIELDS, VALUE

    fov = Step(kind="fov", fov="a", x_um=1.0, y_um=2.0, length=2,
               unit="seconds", repeats=3)
    eng = expand(Routine(steps=[fov, Step(kind="puff")]))[0]
    r.check([(g.start, g.end, g.repeats) for g in eng.groups] == [(0, 1, 3)],
            f"Repeats becomes a group over the whole FOV block ({eng.groups})")
    r.check(len(play_order(eng)) == 7,
            f"…so the engine runs it 3 times ({play_order(eng)})")
    r.check(Routine(steps=[fov]).total_steps() == 3,
            "the panel's run count includes it")

    eng = expand(Routine(steps=[Step(kind="trigger"), fov]))[0]
    trig = [s.kind for s in eng.steps].index("trigger")
    r.check([(g.start, g.end) for g in eng.groups] == [(0, len(eng.steps) - 1)]
            and trig > 0,
            "a Trigger folded into the step waits for an edge on every run")

    grouped = Routine(steps=[fov, replace(fov)],
                      groups=[Group(start=0, end=1, repeats=2)])
    r.check(len(play_order(grouped)) == 4 and len(expand(grouped)[0].groups) == 1,
            "inside a repeat group, the group alone sets the count")
    r.check(expand(Routine(steps=[replace(fov, repeats=1)]))[0].groups == [],
            "control: Repeats of 1 adds no group")

    old = Routine.from_dict({"steps": [{"kind": "fov", "x_um": 1.0}]})
    r.check(old.steps[0].repeats == 1,
            "a routine saved before the column keeps running once")
    back = Routine.from_dict(Routine(steps=[fov]).to_dict())
    r.check(back.steps[0].repeats == 3, "Repeats survives save and load")
    bad = Routine.from_dict({"steps": [{"kind": "fov", "repeats": "x"}]})
    r.check(bad.steps[0].repeats == 1, "a hand-edited bad count falls back to 1")

    panel = SettingsPanel(Routine())
    row = panel.add_fov_step(SimpleNamespace(x_um=1.0, y_um=2.0, z_um=None,
                                             name="a_fov"), Path("a.roi.json"))
    col = FIELDS.index("repeats")
    r.check(panel.settings.steps[row].repeats == 5
            and panel._tbl.item(row, col).text() == "×5",
            f"a new FOV step starts at ×5 "
            f"({panel._tbl.item(row, col).text()!r})")
    made = panel.settings.steps[row]
    r.check(made.trigger, "an FOV step made from a saved FOV has a trigger")
    r.check(panel._tbl.item(row, FIELDS.index("kind")).text() == "FOV + Trigger",
            "…and the table says so")
    kinds = [s.kind for s in expand(Routine(steps=[made]))[0].steps]
    r.check(kinds == ["move", "display", "trigger", "record", "display"],
            f"…waiting after the move, right before the capture ({kinds})")
    eng = expand(Routine(steps=[replace(made, repeats=3)]))[0]
    r.check([(g.start, g.end, g.repeats) for g in eng.groups]
            == [(0, len(eng.steps) - 1, 3)],
            "…inside the repeated block, so every run waits for its own edge")
    both = expand(Routine(steps=[Step(kind="trigger"), made]))[0]
    r.check(sum(s.kind == "trigger" for s in both.steps) == 1,
            "a Trigger row right before one doesn't add a second wait")
    r.check(Routine.from_dict(Routine(steps=[made]).to_dict()).steps[0].trigger,
            "the built-in trigger survives save and load")
    r.check(not old.steps[0].trigger,
            "control: a routine saved before it runs without waiting")
    bad = Routine.from_dict({"steps": [{"kind": "fov", "trigger": "x"}]})
    r.check(bad.steps[0].trigger is False,
            "a hand-edited bad value falls back to no trigger")
    problems = validate(Routine(steps=[made]), RigLimits(has_stage=True,
                                                         x_um=(0.0, 9.0),
                                                         y_um=(0.0, 9.0)))
    r.check(any("trigger" in p for p in problems),
            f"validate refuses it with no camera ({problems})")
    panel._tbl.item(row, col).setData(VALUE, 2.0)
    r.check(panel.settings.steps[row].repeats == 2
            and panel._tbl.item(row, col).text() == "×2",
            "editing the cell sets the step's count")
    panel._r.groups.append(Group(start=0, end=0, repeats=4))
    panel._reload_groups()
    r.check(panel._tbl.item(row, col).text() == "—",
            "a grouped step's cell gives way to its group")
    r.check(panel._spn_g_repeats.value() == 5, "new repeat groups start at ×5")
    panel.deleteLater()
    pump(app, 0.01)


def check_panel_repaint(r: Report, app) -> None:
    """The panel is told its state 30×/s; it must restyle only on a change.
    `setStyleSheet` cost 53 % of the display tick with eight modules loaded.
    Counted, not timed."""
    from acqApp.routines.panel import SettingsPanel

    panel = SettingsPanel()
    styled, texted = [], []
    lbl = panel._lbl_state
    real_style, real_text = lbl.setStyleSheet, lbl.setText
    lbl.setStyleSheet = lambda s: (styled.append(s), real_style(s))[1]
    lbl.setText = lambda s: (texted.append(s), real_text(s))[1]

    for i in range(30):
        panel.set_state(Phase.RUNNING, f"step 1/2 — waiting {i} %")
    r.check(len(styled) == 1,
            f"30 ticks in one phase restyle once ({len(styled)})")
    r.check(len(texted) == 30,
            f"…while the moving text still updates every tick ({len(texted)})")

    panel.set_state(Phase.PAUSED, "PAUSED — the stage stopped answering")
    r.check(len(styled) == 2, "a phase change does restyle")
    r.check(not panel._btn_pause.isEnabled() and panel._btn_resume.isEnabled(),
            "…and the buttons follow the new phase")

    # show_problems() writes the label out of band.
    panel.show_problems(["step 1: nope"])
    panel.set_state(Phase.PAUSED, "PAUSED — the stage stopped answering")
    r.check(len(styled) == 4,
            f"control: an out-of-band write forces the next repaint ({len(styled)})")
    app.processEvents()


def _select_rows(tbl, first: int, last: int) -> None:
    """Select rows first..last, as a shift-click would."""
    from PyQt6.QtWidgets import QTableWidgetSelectionRange
    tbl.clearSelection()
    tbl.setRangeSelected(
        QTableWidgetSelectionRange(first, 0, last, tbl.columnCount() - 1), True)


def _menu_texts(tbl, row: int, span: tuple[int, int] | None = None) -> list[str]:
    """The context menu's action labels for `row`, captured by patching
    `QMenu.exec` instead of blocking on a real popup."""
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu

    if span is not None:
        _select_rows(tbl, span[0], span[1])
    else:
        tbl.select_row(row)
    captured: list[list[str]] = []
    real_exec = QMenu.exec

    def fake_exec(self, *_a, **_k):
        captured.append([act.text() for act in self.actions()])
        return None

    QMenu.exec = fake_exec
    try:
        pos = tbl.visualRect(tbl.model().index(row, 0)).center()
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos)
        tbl.contextMenuEvent(ev)
    finally:
        QMenu.exec = real_exec
    return captured[0] if captured else []


def check_step_table(r: Report, app, tmp: Path) -> None:
    """The step table edits through widgets, gated by the row's kind: a column
    the kind doesn't use renders "—" and refuses editing. The value lives in
    the cell's data; the text only renders it. A row's kind is fixed."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.table import FIELDS, KIND_LABELS, NO_CHANGE, VALUE

    routine = Routine(steps=[Step(kind="wait", label="one", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="two", length=2.0,
                                  unit="seconds")])
    panel = SettingsPanel(routine)
    tbl = panel._tbl
    col = {f: i for i, f in enumerate(FIELDS)}

    def become(row: int, **kw) -> None:
        """Replace step `row` in place (kinds aren't edited in the table)."""
        routine.steps[row] = replace(routine.steps[row], **kw)
        panel._reload_table()

    edits: list = []
    panel.settings_changed.connect(edits.append)

    from PyQt6.QtWidgets import QDoubleSpinBox, QStyleOptionViewItem

    def editor(row: int, field: str):
        c = col[field]
        return tbl.itemDelegateForColumn(c).createEditor(
            tbl, QStyleOptionViewItem(), tbl.model().index(row, c))

    r.check(tbl.itemDelegateForColumn(col["kind"]) is None
            and not (tbl.item(0, col["kind"]).flags()
                     & Qt.ItemFlag.ItemIsEditable),
            "the Kind cell is fixed: no drop-down")
    r.check(tbl.item(0, col["kind"]).text() == KIND_LABELS["wait"],
            "…labelled with KIND_LABELS, not the raw kind string")
    r.check(set(KINDS) <= set(KIND_LABELS),
            "every kind the operator can add has a label")
    ed = editor(0, "settle_s")
    r.check(isinstance(ed, QDoubleSpinBox) and ed.suffix() == " s",
            f"the Settle delegate opens a spin box in seconds regardless of "
            f"the row's kind — editability is a separate gate "
            f"({type(ed).__name__})")
    r.check(isinstance(editor(0, "capture"), QDoubleSpinBox)
            and isinstance(editor(0, "wait"), QDoubleSpinBox),
            "Capture and Wait open spin boxes")
    # The step's name lives on the row header.
    r.check("comment" not in col and "unit" not in col,
            "no Comment column, and no per-row Unit: the routine has one")

    from PyQt6.QtWidgets import QInputDialog

    real_get_text = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("renamed", True))
    try:
        tbl._on_header_double_clicked(0)
    finally:
        QInputDialog.getText = real_get_text
    r.check(routine.steps[0].label == "renamed",
            "double-click on the row header sets the step's name")
    r.check(tbl.verticalHeaderItem(0).text().endswith(": renamed"),
            f"…and the header shows it "
            f"({tbl.verticalHeaderItem(0).text()!r})")
    routine.steps[0].label = "one"     # restore for what follows below
    tbl._paint_numbers()

    # ── kind-gating: a Wait row has only Wait; Settle is FOV/Move only ──
    r.check(bool(tbl.item(0, col["wait"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "Wait is editable on a Wait row")
    r.check(not (tbl.item(0, col["settle_s"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "…and Settle is NOT, on the same row")
    r.check(tbl.item(0, col["wait"]).text() == "100 fr",
            f"Wait renders the value with its unit on a Wait row "
            f"({tbl.item(0, col['wait']).text()!r})")
    r.check(tbl.item(0, col["settle_s"]).text() == "—"
            and tbl.item(0, col["capture"]).text() == "—",
            "Settle and Capture render as a dash on a row that does not use "
            "them")

    become(0, kind="fov", x_um=None, length=100, unit="frames", wait_s=0.0)
    r.check(bool(tbl.item(0, col["settle_s"]).flags() & Qt.ItemFlag.ItemIsEditable)
            and bool(tbl.item(0, col["capture"]).flags()
                     & Qt.ItemFlag.ItemIsEditable),
            "an FOV row edits Settle and Capture")
    r.check(tbl.item(0, col["settle_s"]).text()
            == f"{routine.steps[0].settle_s:g} s"
            and tbl.item(0, col["wait"]).text() == "0 s",
            "…rendering the settle, and its own wait in seconds")

    # ── FOV: the position is one place, not three cells ──
    become(0, x_um=250.0, y_um=None)
    r.check(tbl.item(0, col["fov"]).text() == f"(250 um, {NO_CHANGE})",
            f"FOV renders both axes as one pair "
            f"({tbl.item(0, col['fov']).text()!r})")
    r.check(tbl.item(0, col["fov"]).data(VALUE) == (250.0, None, None),
            "…and the triple is the value of record, not just the text")
    r.check(not (tbl.item(0, col["fov"]).flags() & Qt.ItemFlag.ItemIsEditable),
            "…and is not typed into")
    become(0, x_um=111.0, y_um=222.0, z_um=333.0, fov="window1")
    r.check(tbl.item(0, col["fov"]).text() == "window1",
            f"a saved FOV shows by name ({tbl.item(0, col['fov']).text()!r})")
    r.check(tbl.item(0, col["fov"]).data(VALUE) == (111.0, 222.0, 333.0)
            and "333 um" in tbl.item(0, col["fov"]).toolTip(),
            "…its value and tooltip carrying all three axes")
    become(0, z_um=None)

    # The real handlers would open blocking dialogs offscreen; disconnect them.
    handlers = ((tbl.position_requested, panel._set_position),
                (tbl.pattern_requested, panel._pick_pattern),
                (tbl.fov_requested, panel._pick_fov),
                (tbl.roi_requested, panel._pick_roi))
    opened: list = []
    for sig, slot in handlers:
        sig.disconnect(slot)
    tbl.position_requested.connect(lambda: opened.append("position"))
    tbl.pattern_requested.connect(lambda: opened.append("pattern"))
    tbl.fov_requested.connect(lambda: opened.append("fov"))
    tbl.roi_requested.connect(lambda: opened.append("roi"))
    try:
        tbl._on_double_click(0, col["fov"])
        tbl._on_double_click(0, col["roi"])
        r.check(opened == ["fov", "roi"],
                f"an FOV row's FOV and ROI cells ask for a saved FOV and an "
                f"ROI set ({opened})")
        opened.clear()
        become(1, kind="move")
        tbl._on_double_click(1, col["fov"])
        become(1, kind="display")
        tbl._on_double_click(1, col["roi"])
        r.check(opened == ["position", "pattern"],
                f"…an older Move asks for a position, a Display for a "
                f"pattern ({opened})")
        opened.clear()
        become(1, kind="wait")
        tbl._on_double_click(1, col["fov"])
        tbl._on_double_click(1, col["roi"])
        r.check(opened == [],
                "…and a Wait row has no dialog to ask for at all")
    finally:
        for sig, slot in handlers:
            sig.disconnect()
            sig.connect(slot)

    # QDialog.exec stays patched: restoring it makes every later QDialog
    # .exec() raise TypeError for the rest of the process (a PyQt6/sip quirk),
    # so this must be the last real QDialog.exec this file touches.
    from PyQt6.QtWidgets import QDialog

    QDialog.exec = lambda self: False
    become(1, kind="move", x_um=400.0, y_um=50.0)
    tbl.select_row(1)
    panel._set_position()
    r.check((routine.steps[1].x_um, routine.steps[1].y_um) == (400.0, 50.0),
            "the position dialog, reached for real, changes nothing when "
            "cancelled")
    tbl.setCurrentCell(1, col["fov"])
    QTest.keyClick(tbl, Qt.Key.Key_Delete)
    r.check((routine.steps[1].x_um, routine.steps[1].y_um) == (400.0, 50.0),
            "control: Delete on FOV does nothing — a position is replaced, "
            "not emptied")

    become(1, kind="wait", length=2.0, unit="seconds")
    before_len = routine.steps[1].length
    tbl.setCurrentCell(1, col["wait"])
    QTest.keyClick(tbl, Qt.Key.Key_Delete)
    r.check(routine.steps[1].length == before_len,
            f"control: Delete on a cell that must hold a number does "
            f"nothing ({routine.steps[1].length})")
    try:
        tbl.clear_cell(1, "roi")
        r.check(True, "control: clearing ROI on a Wait row is a silent "
                     "no-op, not a crash")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped clearing a Wait row's "
                      f"ROI: {e}")

    tbl.item(0, col["capture"]).setData(VALUE, 100.4)
    r.check(routine.steps[0].length == 100,
            f"a fractional capture on a frames step is rounded "
            f"({routine.steps[0].length})")
    r.check(not [p for p in validate(routine, RigLimits(
                has_frames=True, has_dmd=True, has_stage=True,
                x_um=(-1e4, 1e4), y_um=(-1e4, 1e4)))
                if "whole number" in p],
            "…so the routine validates instead of being refused at Start")
    routine.steps[0].unit = "seconds"
    tbl.item(0, col["capture"]).setData(VALUE, 2.5)
    r.check(routine.steps[0].length == 2.5,
            f"control: a seconds capture keeps its fraction "
            f"({routine.steps[0].length})")
    become(0, kind="wait", label="one", length=100, unit="frames")

    panel._tbl.select_row(0)
    panel._move_down()
    r.check([x.label for x in routine.steps] == ["two", "one"],
            f"a step moves down the list ({[x.label for x in routine.steps]})")
    r.check(panel._tbl.selected_row() == 1,
            "…and the selection follows it, so a second press moves the same step")
    panel._move_up()
    r.check([x.label for x in routine.steps] == ["one", "two"],
            "…and back up again")

    become(0, kind="display", pattern=r"C:\patterns\grid.png")
    r.check(tbl.item(0, col["roi"]).text() == "grid.png",
            f"the ROI cell shows the file's name "
            f"({tbl.item(0, col['roi']).text()!r})")
    r.check(not (tbl.item(0, col["roi"]).flags()
                & Qt.ItemFlag.ItemIsEditable),
            "…and is not typed into")
    panel._tbl.select_row(0)
    panel._tbl.setCurrentCell(0, col["roi"])
    panel._clear_pattern()
    r.check(routine.steps[0].pattern == "" and
            tbl.item(0, col["roi"]).text() == "stop displaying",
            "clearing an older Display's pattern reads as 'stop displaying', "
            "not a blank cell")
    r.check(tbl.item(0, col["roi"]).icon().isNull(),
            "…and the thumbnail clears with it")

    from PyQt6.QtGui import QPixmap

    real_pattern = tmp / "real_pattern.png"
    QPixmap(8, 8).save(str(real_pattern), "PNG")
    routine.steps[0].pattern = str(real_pattern)
    panel._reload_table()
    r.check(not tbl.item(0, col["roi"]).icon().isNull(),
            "a step with a real pattern image shows a thumbnail")

    from acqApp.devices.dmd import roi_store
    from acqApp.devices.dmd.roi import CircleRoi, RoiSet

    roi_set = RoiSet([CircleRoi(x=50.0, y=50.0, r=20.0)])
    roi_path = roi_store.save("some_set", roi_set)
    routine.steps[0].pattern = str(roi_path)
    panel._reload_table()
    r.check(tbl.item(0, col["roi"]).text() == "some_set",
            f"an ROI set names itself ({tbl.item(0, col['roi']).text()!r})")
    r.check(not tbl.item(0, col["roi"]).icon().isNull(),
            "…and shows a thumbnail of its shapes")

    empty_path = tmp / "empty_set.roi.json"
    empty_path.write_text(
        json.dumps({"name": "empty_set", "rois": []}), encoding="utf-8")
    routine.steps[0].pattern = str(empty_path)
    panel._reload_table()
    r.check(tbl.item(0, col["roi"]).icon().isNull(),
            "control: an ROI set with nothing in it gets no thumbnail")
    corrupt_path = tmp / "corrupt_set.roi.json"
    corrupt_path.write_text("{not json", encoding="utf-8")
    routine.steps[0].pattern = str(corrupt_path)
    try:
        panel._reload_table()
        r.check(True, "control: a corrupt ROI file does not crash the repaint")
    except Exception as e:                        # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped a corrupt ROI file: {e}")

    routine.steps[0].pattern = r"C:\patterns\grid.png"
    panel._refresh_summary()
    text = panel._lbl_summary.text()
    r.check("2 run(s)" in text and "emit light" in text,
            f"the summary says how much work it is and that it emits light "
            f"({text!r})")
    routine.steps[0].pattern = ""
    panel._refresh_summary()
    r.check("emit light" not in panel._lbl_summary.text(),
            f"control: with nothing projecting it does not warn "
            f"({panel._lbl_summary.text()!r})")

    panel.set_state(Phase.RUNNING, "step 2/2", 1)
    r.check(tbl.item(1, 0).font().bold() and not tbl.item(0, 0).font().bold(),
            "the running step is bold in the table")
    panel.set_state(Phase.DONE, "finished", None)
    r.check(not tbl.item(1, 0).font().bold(),
            "…and nothing is bold once it is over")

    # ── the right-click menu is gated by the row's kind ──
    become(0, kind="fov", pattern="")
    texts = _menu_texts(tbl, 0)
    r.check(any("Change FOV" in t for t in texts)
            and any("Set ROI" in t for t in texts)
            and any("No light" in t for t in texts),
            f"an FOV row offers FOV, ROI set and No light ({texts})")

    become(0, kind="move")
    texts = _menu_texts(tbl, 0)
    r.check(any("Set position" in t for t in texts)
            and any("Fill Stage" in t for t in texts)
            and not any("pattern" in t.lower() for t in texts),
            f"an older Move row offers position/FOV actions, not pattern ones "
            f"({texts})")

    become(0, kind="display")
    texts = _menu_texts(tbl, 0)
    r.check(any("Set pattern" in t for t in texts)
            and any("Set ROI" in t for t in texts)
            and not any("position" in t.lower() for t in texts)
            and not any("FOV" in t for t in texts),
            f"an older Display row offers pattern/ROI actions, not position "
            f"ones ({texts})")

    become(0, kind="wait")
    texts = _menu_texts(tbl, 0)
    r.check(not any("pattern" in t.lower() or "position" in t.lower()
                   or "fov" in t.lower() for t in texts),
            f"a Wait row offers neither — only Duplicate/Remove ({texts})")
    r.check(any("Duplicate" in t for t in texts)
            and any("Remove" in t for t in texts),
            f"…which every row keeps, regardless of kind ({texts})")

    r.check(not any("Group selected" in t for t in texts),
            "with only one row selected, no group action is offered")
    texts2 = _menu_texts(tbl, 0, span=(0, 1))
    r.check(any("Group selected steps 1-2" in t for t in texts2),
            f"2+ contiguous rows selected offers Group selected ({texts2})")
    r.check(not any("recording" in t.lower() for t in texts2),
            "recording is never on the context menu — it's the row header "
            "click, not a selection action")

    app.processEvents()


def check_group_panel(r: Report, app) -> None:
    """Repeat groups are added/removed by selecting table rows; a recording
    is a `record` step, not a selection."""
    from acqApp.routines.panel import SettingsPanel

    routine = Routine(steps=[Step(kind="wait", label="A"),
                             Step(kind="wait", label="B"),
                             Step(kind="wait", label="C")])
    panel = SettingsPanel(routine)

    r.check(not panel._btn_g_add.isEnabled(),
            "control: nothing selected -> no Group offered")

    # ── recordings ──
    r.check(routine.recordings == [], "control: no Record step, no recording")
    routine.steps[0].kind = "record"
    panel._reload_table()
    r.check(routine.recordings == [Recording(start=0, end=0)],
            f"a Record step is a one-step Recording ({routine.recordings})")
    r.check(panel._tbl._recording_at(0) is routine.steps[0]
            and panel._tbl._recording_at(1) is None,
            "…and the table knows which row is recording")
    routine.steps[1].kind = routine.steps[2].kind = "record"
    panel._reload_table()
    r.check(routine.recordings == [Recording(start=0, end=0),
                                   Recording(start=1, end=1),
                                   Recording(start=2, end=2)],
            f"each Record step is its own Recording, never a merged range "
            f"({routine.recordings})")
    for st in routine.steps:
        st.kind = "wait"
    panel._reload_table()
    r.check(routine.recordings == [], "back to Waits: no recordings")

    # ── repeat groups ──
    panel._tbl.select_row(0)
    r.check(not panel._btn_g_add.isEnabled(),
            "a single selected row is still not a repeat group — one step "
            "repeated in place is what a Wait's own length already says")

    _select_rows(panel._tbl, 1, 2)              # steps B, C (0-based 1..2)
    r.check(panel._btn_g_add.isEnabled(),
            "2+ contiguous rows selected -> Group selected is offered")
    r.check("2-3" in panel._lbl_g_selection.text(),
            f"…and the selection is named in 1-based step numbers "
            f"({panel._lbl_g_selection.text()!r})")

    panel._spn_g_repeats.setValue(4)
    panel._group_selected()
    r.check(len(routine.groups) == 1 and routine.groups[0].start == 1
            and routine.groups[0].end == 2 and routine.groups[0].repeats == 4,
            f"the table's own (0-based) selection becomes the Group "
            f"({routine.groups})")
    r.check(panel._lst_groups.count() == 1
            and "2-3" in panel._lst_groups.item(0).text()
            and "4" in panel._lst_groups.item(0).text(),
            f"…and the list shows it ({panel._lst_groups.item(0).text()!r})")
    r.check(panel._tbl._group_at(1) is routine.groups[0]
            and panel._tbl._group_at(2) is routine.groups[0]
            and panel._tbl._group_at(0) is None,
            "…and the table itself knows which rows are grouped")

    routine.steps[1].kind = "record"
    panel._reload_table()
    r.check(routine.recordings == [Recording(start=1, end=1)]
            and panel._tbl._group_at(1) is routine.groups[0],
            "a Record step nests inside a group without disturbing it")

    reloaded = Routine.from_dict(routine.to_dict())
    r.check(len(reloaded.groups) == 1 and reloaded.groups[0].repeats == 4,
            "a saved template keeps its repeat group")
    r.check(reloaded.recordings == [Recording(start=1, end=1)],
            "…and its Record step")
    routine.steps[1].kind = "wait"               # clean up for what follows
    panel._reload_table()

    from PyQt6.QtWidgets import QInputDialog

    real_get_int = QInputDialog.getInt
    QInputDialog.getInt = staticmethod(lambda *a, **k: (7, True))
    try:
        panel._edit_group_repeats(panel._lst_groups.item(0))
    finally:
        QInputDialog.getInt = real_get_int
    r.check(routine.groups[0].repeats == 7,
            f"double-clicking a group lets its repeat count be changed "
            f"({routine.groups[0].repeats})")

    QInputDialog.getInt = staticmethod(lambda *a, **k: (99, False))
    try:
        panel._edit_group_repeats(panel._lst_groups.item(0))
    finally:
        QInputDialog.getInt = real_get_int
    r.check(routine.groups[0].repeats == 7,
            "control: cancelling the repeat-count dialog changes nothing")

    panel._lst_groups.setCurrentRow(0)
    panel._del_group()
    r.check(routine.groups == [] and panel._lst_groups.count() == 0,
            "Remove selected clears the group from both the routine and the "
            "list")
    r.check(panel._tbl._group_at(1) is None,
            "…and the table stops tinting/badging those rows")

    _select_rows(panel._tbl, 0, 1)
    panel._tbl.group_requested.emit()
    r.check(len(routine.groups) == 1 and routine.groups[0].start == 0
            and routine.groups[0].end == 1,
            f"the context menu's Group action reaches the same handler "
            f"({routine.groups})")
    panel._lst_groups.setCurrentRow(0)
    panel._del_group()

    # set_routine (template load) replaces groups and recordings too.
    _select_rows(panel._tbl, 0, 1)
    panel._group_selected()
    routine.steps[0].kind = "record"
    panel._reload_table()
    other = Routine(steps=[Step(kind="wait"), Step(kind="wait")])
    panel.set_routine(other)
    r.check(panel.settings.groups == [] and panel.settings.recordings == [],
            "loading a template with neither clears the panel's own")
    app.processEvents()


def check_per_edge_routine_is_buildable(r: Report, app) -> None:
    """The operator's per-edge protocol ("100 recordings at FOV 1, one per
    edge"), built through the UI: [move, trigger, record], group on the
    last two."""
    from acqApp.routines.panel import SettingsPanel

    routine = Routine(steps=[Step(kind="move", label="FOV 1", x_um=0.0,
                                  y_um=0.0, settle_s=0.0),
                             Step(kind="trigger", label="edge"),
                             Step(kind="record", label="capture", length=5.0,
                                  unit="seconds")])
    panel = SettingsPanel(routine)

    _select_rows(panel._tbl, 1, 2)
    panel._spn_g_repeats.setValue(100)
    panel._group_selected()
    r.check(len(routine.groups) == 1 and routine.groups[0].start == 1
            and routine.groups[0].end == 2
            and routine.groups[0].repeats == 100,
            f"the trigger+wait pair repeats 100x, leaving the move outside "
            f"({routine.groups})")

    r.check(routine.recordings == [Recording(start=2, end=2)],
            f"the Record step is the only recording ({routine.recordings})")

    built = panel.settings
    r.check(built.save_mode == "per_repeat" and built.cycles == 1,
            f"the panel always runs one pass, a file per edge "
            f"({built.cycles}, {built.save_mode})")
    problems = validate(built, FULL_RIG)
    r.check(problems == [],
            f"the assembled per-edge routine validates clean ({problems})")

    reloaded = Routine.from_dict(built.to_dict())
    r.check(reloaded.steps[1].kind == "trigger"
            and reloaded.groups[0].repeats == 100
            and reloaded.recordings[0].start == reloaded.recordings[0].end == 2,
            "…and round-trips through a saved template intact")


def check_move_row_repaint(r: Report) -> None:
    """A reorder repaints only the rows between src and dest, not the whole
    table."""
    from acqApp.routines.panel import SettingsPanel

    routine = Routine(steps=[Step(kind="wait", label=c, length=1, unit="frames")
                             for c in "ABCDE"])
    panel = SettingsPanel(routine)
    tbl = panel._tbl

    touched: list[int] = []
    real_paint_row = tbl._paint_row
    tbl._paint_row = lambda row, s: (touched.append(row), real_paint_row(row, s))[1]

    tbl.move_row(0, 1)                  # adjacent — only rows 0,1 shifted
    r.check(sorted(set(touched)) == [0, 1],
            f"an adjacent move repaints only the two rows involved, not all "
            f"5 ({sorted(set(touched))})")
    r.check([s.label for s in routine.steps] == list("BACDE"),
            f"…and the reorder itself is still correct ({[s.label for s in routine.steps]})")

    touched.clear()
    tbl.move_row(4, 0)                  # far move — every row in between shifts
    r.check(sorted(set(touched)) == [0, 1, 2, 3, 4],
            f"a move spanning the whole table repaints every row it actually "
            f"shifted ({sorted(set(touched))})")
    r.check([s.label for s in routine.steps] == list("EBACD"),
            f"…correctly ({[s.label for s in routine.steps]})")

    tbl._paint_row = real_paint_row


# ── the template library ──────────────────────────────────────────────────────

def check_templates(r: Report) -> None:
    """A protocol worth running twice survives as a file, not as a config key."""
    from acqApp.routines import templates

    r.check(templates.names() == [],
            f"an empty library lists nothing ({templates.names()})")

    rt = Routine(name="grid 3x3", cycles=2, save_mode="per_repeat",
                 steps=[Step(kind="move", label="a", x_um=100.0),
                        Step(kind="wait", label="b", length=1.5, unit="seconds")])
    path = templates.save(rt)
    r.check(path.exists() and path.name.endswith(templates.SUFFIX),
            f"saving writes one file ({path.name})")
    r.check(templates.names() == ["grid 3x3"],
            f"…which the library then lists ({templates.names()})")

    back = templates.load("grid 3x3")
    r.check([vars(x) for x in back.steps] == [vars(x) for x in rt.steps],
            "every field of every step comes back")
    r.check(back.cycles == 2 and back.save_mode == "per_repeat"
            and back.name == "grid 3x3",
            f"…and so do the cycles and the save mode "
            f"({back.cycles}, {back.save_mode})")

    # The routine name is the operator's free text, aimed at the filesystem.
    evil = "../../evil/name"
    r.check(templates.path_for(evil).parent == templates.DIR
            and templates.path_for("   ").name.startswith("routine"),
            f"a name is not a path — it cannot escape the folder "
            f"({templates.path_for(evil).name!r})")
    back_evil = "..\\..\\evil\\name"
    r.check(templates.path_for(back_evil).parent == templates.DIR
            and "\\" not in templates.safe_name(back_evil),
            f"…nor with Windows separators "
            f"({templates.safe_name(back_evil)!r})")

    templates.save(Routine(name="b"), "b")
    templates.save(Routine(name="A"), "A")
    r.check(templates.names() == ["A", "b", "grid 3x3"],
            f"the library is sorted the way a human reads it "
            f"({templates.names()})")

    # A stale template must not stop the app: from_dict migrates what it can
    # and validate() refuses the rest at Start.
    templates.path_for("stale").write_text(
        '{"name": "stale", "cycles": "lots", "save_mode": "nope",'
        ' "steps": [{"label": "ok", "length": 5, "unit": "seconds"},'
        ' {"gone": 1}, "not a step"]}', encoding="utf-8")
    stale = templates.load("stale")
    r.check(len(stale.steps) == 2 and stale.cycles == 1
            and stale.save_mode == "single",
            f"a stale template migrates both old-format steps and drops "
            f"the one that was not a dict at all "
            f"({len(stale.steps)} step(s), cycles={stale.cycles})")
    r.check(len(stale.recordings) == 2,
            "…each migrated step wrapped in its own auto-generated Recording")

    templates.delete("b")
    r.check("b" not in templates.names() and "A" in templates.names(),
            f"deleting removes one and only one ({templates.names()})")
    templates.delete("b")               # a second delete must not raise


def check_panel_tracker(r: Report, app) -> None:
    """The operator's panel asks, at the widget level: progress, reordering,
    cost up front, a +Step kind picker, and a template library."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from acqApp.routines import templates
    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.table import RUNNING

    routine = Routine(name="tracked",
                      steps=[Step(kind="wait", label="one", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="two", length=100,
                                  unit="frames"),
                             Step(kind="wait", label="three", length=100,
                                  unit="frames")])
    panel = SettingsPanel(routine)
    tbl = panel._tbl

    # ── 1. where the routine is ──
    r.check(panel._bar.isHidden(),
            "before a run there is no progress bar — an empty one reads as a "
            "stalled run")
    values: list[int] = []
    real = panel._bar.setValue
    panel._bar.setValue = lambda v: (values.append(v), real(v))[1]
    panel.set_progress(0.25, "1 s elapsed")
    r.check(not panel._bar.isHidden() and values == [250],
            f"…and starting one shows it at where it is ({values})")
    for _ in range(30):
        panel.set_progress(0.25, "1 s elapsed")
    r.check(values == [250],
            f"30 ticks at the same place repaint the bar once ({len(values)})")
    panel.set_progress(0.5, "2 s elapsed")
    r.check(values == [250, 500] and panel._lbl_eta.text() == "2 s elapsed",
            f"…and it follows the routine when it moves ({values})")
    panel.set_progress(None)
    r.check(panel._bar.isHidden() and panel._lbl_eta.isHidden(),
            "a routine that is no longer running puts both away")

    panel.set_state(Phase.RUNNING, "step 2/3", 1)
    heads = [tbl.verticalHeaderItem(i).text() for i in range(tbl.rowCount())]
    r.check(heads == ["1: one", f"{RUNNING}: two", "3: three"],
            f"the running step is marked in the row header ({heads})")
    panel.set_state(Phase.DONE, "finished", None)
    heads = [tbl.verticalHeaderItem(i).text() for i in range(tbl.rowCount())]
    r.check(heads == ["1: one", "2: two", "3: three"],
            f"…and the headers are the step order again once it is over "
            f"({heads})")

    # ── 2. reordering ──
    tbl.move_row(0, 2)
    r.check([x.label for x in routine.steps] == ["two", "three", "one"],
            f"a step moves straight to a row, not one press at a time "
            f"({[x.label for x in routine.steps]})")
    r.check(tbl.selected_row() == 2,
            "…and the selection follows the step it moved")
    QTest.keyClick(tbl, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    r.check([x.label for x in routine.steps] == ["two", "one", "three"],
            f"Ctrl+Up moves it from the keyboard "
            f"({[x.label for x in routine.steps]})")
    before = [x.label for x in routine.steps]
    QTest.keyClick(tbl, Qt.Key.Key_Up)
    r.check([x.label for x in routine.steps] == before,
            f"control: the arrow key on its own still just moves the cursor "
            f"({[x.label for x in routine.steps]})")
    r.check(not tbl.move_row(0, -5) and not tbl.move_row(9, 0)
            and [x.label for x in routine.steps] == before,
            "a move off either end of the list does nothing")
    r.check(tbl._steps is panel.settings.steps,
            "the table reorders the routine's own step list, not a copy of it")

    # ── 3. what it will cost ──
    panel.set_frame_rate(None)
    r.check("at least" in panel._lbl_summary.text()
            and "300 frames" in panel._lbl_summary.text(),
            f"with no camera loaded the summary counts frames as frames "
            f"({panel._lbl_summary.text()!r})")
    panel.set_frame_rate(100.0)
    text = panel._lbl_summary.text()
    r.check("about" in text and "100 Hz" in text and "frames" not in text,
            f"…and a loaded camera turns the whole protocol into a duration, "
            f"naming the rate it used ({text!r})")
    r.check(panel.frame_rate == 100.0,
            "the panel keeps the rate, so the run readout does not re-ask the "
            "camera 30 times a second")

    r.check(not hasattr(panel, "_cmb_trigger") and not hasattr(routine,
                                                               "start_trigger"),
            "the manual/TTL choice is gone — every routine arms on Start")

    # ── Other ▸ Wait / Puff / Trigger ──
    n_before = len(routine.steps)
    panel._add_step("puff")
    r.check(len(routine.steps) == n_before + 1
            and routine.steps[-1].kind == "puff",
            f"Other ▸ appends a step of the picked kind "
            f"({routine.steps[-1].kind!r})")
    # Not list.pop(): that leaves the selection dangling, and the
    # _del_step() calls below would silently no-op.
    panel._del_step()

    # ── 4. the library ──
    panel.save_template("saved one")
    r.check("saved one" in templates.names(),
            f"Save as… writes the protocol to the library "
            f"({templates.names()})")
    r.check(panel._cmb_tpl.currentText() == "saved one",
            "…and selects it, so Load next means what was just saved")

    panel._del_step()
    panel._del_step()
    r.check(len(routine.steps) == 1, "the protocol is then edited down")
    panel.load_template("saved one")
    r.check([x.label for x in panel.settings.steps] == before,
            f"Load puts the saved protocol back, in its saved order "
            f"({[x.label for x in panel.settings.steps]})")
    r.check(tbl._steps is panel.settings.steps and tbl.rowCount() == 3,
            "…into the same list the table edits — a loaded template is the "
            "routine, not a second one")

    emitted: list = []
    panel.settings_changed.connect(emitted.append)
    panel.load_template("saved one")
    r.check(len(emitted) == 1,
            f"loading persists the routine ONCE, not once per widget it moved "
            f"({len(emitted)})")

    said: list = []
    panel.status_message.connect(said.append)
    panel.load_template("no such template")
    r.check(said and "could not load" in said[-1],
            f"control: loading a missing template says so ({said})")

    panel._cmb_tpl.setCurrentText("saved one")
    panel._on_delete_template()
    r.check("saved one" not in templates.names(),
            f"Delete removes it from the library ({templates.names()})")
    r.check(len(panel.settings.steps) == 3,
            "…and leaves the protocol being edited alone")
    app.processEvents()


# ── the whole app ─────────────────────────────────────────────────────────────

def check_app(r: Report, app, tmp) -> None:
    """The real window in mock mode: the panel refuses, the engine drives
    real adapters through `stage_target`/`pattern_target`, and recording
    boundaries reach the file on the shared clock."""
    import csv

    out = tmp / "routine_rec"
    win = make_window({"voltage_cam", "stage", "dmd", "routines"})
    mod = {m.key: m for m in win._modules}
    adapter, panel = mod["routines"], mod["routines"].panel

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_mouse_id.setText("routine")
    win._save_panel._on_edited()

    r.check(win.stage_target() is mod["stage"],
            "the window offers the loaded stage as a routine target, even "
            "before Live view starts")
    r.check(win.pattern_target() is mod["dmd"],
            "…and the loaded DMD as a pattern target")
    win._btn_run.setChecked(True)

    ctrl0 = mod["stage"].controller
    real_stop_all = ctrl0.stop_all

    def boom() -> None:
        raise RuntimeError("serial link gone")

    ctrl0.stop_all = boom
    try:
        win.stage_target().stop_motion()
        r.check(True, "stop_motion() survives a controller that raises")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped stop_motion: {e}")
    try:
        ctrl0.stop_all()
        r.check(False, "control: the raw controller call must still raise")
    except RuntimeError:
        r.check(True, "control: the raw controller call does raise")
    ctrl0.stop_all = real_stop_all

    (lo_x, hi_x), (lo_y, hi_y) = win.stage_target().limits_um()
    inside = lo_x + (hi_x - lo_x) * 0.37
    inside_y = lo_y + (hi_y - lo_y) * 0.62

    st = win.stage_target()
    r.check(not st.is_moving(), "is_moving: false before any move")
    st.move_to(inside, inside_y)
    r.check(st.is_moving(), "is_moving: true right after a move is issued")
    for _ in range(200):
        if not st.is_moving():
            break
        pump(app, 0.01)
    r.check(not st.is_moving(), "is_moving: false once the stage has arrived")

    app_pattern = tmp / "app.png"
    app_pattern.write_bytes(b"x")
    panel._r.steps = [
        Step(kind="move", label="one", x_um=inside, settle_s=0.0),
        Step(kind="record", label="frames-record", length=5, unit="frames"),
        Step(kind="move", label="two", x_um=inside, y_um=inside_y,
             settle_s=0.05),
        Step(kind="display", label="show", pattern=str(app_pattern)),
        Step(kind="record", label="seconds-record", length=0.30, unit="seconds"),
    ]
    panel._r.save_mode = "single"   # both runs in one file to read back; rolling: check_file_rolling
    panel._reload_table()

    r.check(not win._btn_rec.isChecked(), "fixture: not recording yet")
    adapter._start()
    r.check(adapter._engine is not None,
            "Start runs the routine without the operator pressing Record first")
    r.check(win._btn_rec.isChecked(), "…by opening the recording itself")
    r.check(adapter._own_rec, "…and it knows that recording is its own")
    adapter._abort()
    r.check(not win._btn_rec.isChecked(),
            "ending the routine stops the recording it started")

    # The Start button asks first; Cancel starts nothing, OK runs as above.
    from acqApp.adapters.routines import start_summary
    text = start_summary("m1", Path("D:/rec/m1/20261007"), "full", 2, 100.0,
                         "DCIMG")
    r.check(all(s in text for s in ("m1", "D:", "full", "binning 2",
                                    "100 Hz", "DCIMG", "LED")),
            "the start summary names mouse, path, camera settings and the LED")

    # ROI sets reaching past the capture area (512-row band: rows 928..1440).
    from acqApp.adapters.routines import rois_beyond_capture
    from acqApp.devices.dmd import roi_store
    from acqApp.devices.dmd.roi import RectRoi, RoiSet
    roi_in = roi_store.save("in_roi", RoiSet([RectRoi(name="a", x=500, y=1000,
                                                      w=50, h=50)]))
    roi_past = roi_store.save("past_roi", RoiSet([
        RectRoi(name="a", x=500, y=1000, w=50, h=50),
        RectRoi(name="b", x=500, y=200, w=50, h=50)]))
    shows = Routine(steps=[Step(kind="display", pattern=str(roi_in)),
                           Step(kind="display", pattern=str(roi_past)),
                           Step(kind="display", pattern=str(roi_past))])
    beyond = rois_beyond_capture(shows, "4432x512")
    r.check(beyond == ["ROI: past_roi: b"],
            f"Start names the ROI past a band's edge, once per set ({beyond})")
    r.check(rois_beyond_capture(shows, "full") == []
            and rois_beyond_capture(shows, None) == [],
            "…and nothing at full frame or with no camera")
    warned = start_summary("m1", Path("D:/rec"), "4432x512", 1, None, "DCIMG",
                           beyond)
    r.check("WARNING" in warned and "past_roi: b" in warned,
            "…in the start popup's text")
    adapter._confirm_start = lambda: False
    adapter._on_start_clicked()
    r.check(adapter._engine is None, "Cancel on the start popup starts nothing")
    adapter._confirm_start = lambda: True
    adapter._on_start_clicked()
    r.check(adapter._engine is not None, "OK on the start popup starts the routine")
    adapter._abort()
    del adapter._confirm_start

    # A manual recording is Internal-mode and a routine won't switch mode
    # under it, so it refuses rather than adopt it (2026-09-28).
    win._btn_rec.setChecked(True)
    adapter._start()
    r.check(adapter._engine is None,
            "a manual (necessarily Internal-mode) recording already running "
            "refuses the routine rather than silently adopting the wrong mode")
    r.check(win._btn_rec.isChecked(),
            "…leaving the operator's own recording running, untouched")
    win._btn_rec.setChecked(False)              # clean slate for the real run
    pump(app, 0.1)

    panel._r.steps[0].x_um = hi_x + 10_000.0
    r.check(any("soft limits" in p
                for p in validate(panel.settings, adapter._rig())),
            "…and an out-of-limits target is refused before the run, not at step 7")
    panel._r.steps[0].x_um = inside

    moved: list[tuple] = []
    lit: list[bool] = []
    ctrl = mod["stage"].controller
    real_move = ctrl.move_to_um
    ctrl.move_to_um = lambda which, um: (moved.append((which, um)),
                                         real_move(which, um))[1]
    real_light = mod["dmd"].set_light
    mod["dmd"].set_light = lambda on: (lit.append(bool(on)), real_light(on))[1]

    adapter._start()
    if not r.check(adapter._engine is not None,
                   "the routine starts and opens its own recording"):
        return
    path = win._rec_path
    r.check(path is not None, "…a session file actually exists")
    eng0 = adapter._engine       # the session stops at DONE and drops it

    r.check("routine" in adapter.busy_reason().lower(),
            f"the adapter declares itself busy while a routine runs "
            f"({adapter.busy_reason()!r})")
    try:
        win.set_modules({"voltage_cam"})
        r.check(False, "set_modules must be refused while a routine runs")
    except RuntimeError as e:
        r.check(True, f"set_modules is refused while a routine runs ({e})")

    # A shorter template loaded mid-run used to IndexError the engine.
    adapter.update_display()
    r.check(adapter._routine is not panel._r
            and len(adapter._routine.steps) == len(panel._r.steps),
            "the engine runs a copy of the protocol, not the panel's object")
    r.check(not panel._protocol.isEnabled() and not panel._setup.isEnabled(),
            "…and every protocol edit (templates, ±Step, groups) is locked")

    for _ in range(200):
        win._display_tick()
        pump(app, 0.02)
        if eng0.phase == Phase.DONE:
            break
    adapter.update_display()
    r.check(panel._protocol.isEnabled() and panel._setup.isEnabled(),
            "…unlocked again once it is done")

    phase, runs, filed, steps_done = (eng0.phase, list(eng0.runs),
                                      adapter._filed, eng0.steps_done())
    win._btn_rec.setChecked(False)
    win._btn_run.setChecked(False)
    win.close()
    pump(app, 0.2)

    r.check(phase == Phase.DONE, f"the routine ran to the end (phase={phase})")
    r.check(len(runs) == 2 and not any(x.interrupted for x in runs),
            f"both recording brackets completed cleanly ({len(runs)} runs)")
    r.check(steps_done == 5,
            f"all five atomic steps completed ({steps_done})")
    r.check(("x", inside) in moved and ("y", inside_y) in moved,
            f"the stage really was commanded through the host ({moved})")
    r.check(sum(1 for a, _ in moved if a == "y") == 1,
            f"…and an axis left blank in a step is left where it is "
            f"({[a for a, _ in moved]})")
    r.check(lit and lit[-1] is False and True in lit,
            f"the light went on for the projecting step and off after ({lit})")

    frames_run = runs[0]
    r.check(frames_run.frames is not None and frames_run.frames >= 5,
            f"the first Record step (5 frames) counted frames "
            f"that reached the FILE ({frames_run.frames})")
    r.check(filed == 4, f"four recording boundaries were filed (got {filed})")

    mod["stage"].busy_reason = lambda: "the stage says no"
    try:
        win.set_modules({"voltage_cam"})
        r.check(False, "control: a busy adapter must block set_modules")
    except RuntimeError as e:
        r.check("stage says no" in str(e),
                f"control: the window really asks every adapter ({e})")
    mod["stage"].busy_reason = lambda: ""

    stem = path.name
    with open(path / f"{stem}_data.csv", encoding="utf-8") as f:
        rows = [row for row in csv.DictReader(f) if row["stream"] == "routine"]
    r.check(bool(rows), "routine rows are in the session's data CSV")
    ts = [float(row["timestamp"]) for row in rows]
    vals = [float(row["value"]) for row in rows]
    r.check(bool(ts) and all(b >= a for a, b in zip(ts, ts[1:])) and ts[0] >= 0.0,
            f"…stamped on the session clock, in order "
            f"({[round(v, 2) for v in ts]})")
    r.check(len(vals) == 4,
            f"one entry per boundary, opening and closing each Recording "
            f"({vals})")
    r.check(len(vals) >= 2 and vals[0] > 0 and vals[1] < 0,
            f"…the sign says which edge it is, keyed on `region` "
            f"({vals[:2]})")
    with open(path / f"{stem}_settings.json", encoding="utf-8") as f:
        a = json.load(f)
    r.check(a.get("routine_started") is True,
            "the file says a routine actually ran")
    r.check(int(a.get("routine_steps_done", 0)) == 5,
            f"…and how many ATOMIC steps finished "
            f"({a.get('routine_steps_done')})")
    r.check(int(a.get("routine_recordings_interrupted", -1)) == 0,
            "…and that no recording bracket was interrupted")
    r.check(int(a.get("routine_n_steps", 0)) == 5
            and "one" in str(a.get("routine_steps", "")),
            "…and carries the protocol itself, which nothing else records")
    run_attrs = a.get("routine_runs", [])
    if isinstance(run_attrs, str):
        run_attrs = json.loads(run_attrs)
    r.check(len(run_attrs) == 2
            and [x["routine_recording_region"] for x in run_attrs] == [0, 1],
            f"…and every RECORDING execution, not just the counts "
            f"({len(run_attrs)})")
    r.check(len(run_attrs) == 2
            and all(x["routine_recording_interrupted"] is False
                    for x in run_attrs)
            and (run_attrs[1]["routine_recording_t0"]
                 > run_attrs[0]["routine_recording_t0"]),
            "…each with its own t0 on the shared clock and its fault flag")


def check_file_rolling(r: Report, app, tmp) -> None:
    """save_mode "per_repeat"/"per_group" really roll to new files through
    the real window's `roll_recording()`."""
    out = tmp / "routine_roll"
    # voltage_cam only because every routine arms on the camera.
    win = make_window({"stage", "routines", "voltage_cam"})
    mod = {m.key: m for m in win._modules}
    adapter, panel = mod["routines"], mod["routines"].panel

    win._save_panel._ed_folder.setText(str(out))
    win._save_panel._ed_mouse_id.setText("roll")
    win._save_panel._on_edited()

    def run_to_done(steps, groups, save_mode) -> int:
        """Run one routine to completion, -> how many NEW recordings
        (session folders) appeared."""
        before = set(out.rglob("*_settings.json"))
        panel._r.steps = steps
        panel._r.groups = groups
        panel._r.cycles = 1
        panel._r.save_mode = save_mode      # the panel pins per_repeat
        panel._reload_table()
        adapter._start()
        if not r.check(adapter._engine is not None,
                       f"[{save_mode}] the routine starts "
                       f"({panel._lbl_state.text() if adapter._engine is None else ''})"):
            return 0
        eng0 = adapter._engine   # the session stops at DONE and drops it
        for _ in range(400):
            win._display_tick()
            pump(app, 0.01)
            if eng0.phase == Phase.DONE:
                break
        r.check(eng0.phase == Phase.DONE,
                f"[{save_mode}] the routine ran to the end "
                f"(phase={eng0.phase}, fault={eng0.fault!r})")
        return len(set(out.rglob("*_settings.json")) - before)

    n = run_to_done(
        [Step(kind="move", label="m", x_um=10.0, settle_s=0.0),
         Step(kind="record", label="w", length=0.02, unit="seconds")],
        [Group(start=0, end=1, repeats=3)], "per_repeat")
    r.check(n == 3, f"per_repeat: one file per repeat of the group ({n})")

    # Two groups x2 repeats: per_group makes 2 files, not 4.
    multi_group_steps = [
        Step(kind="move", label="a", x_um=10.0, settle_s=0.0),
        Step(kind="record", label="wa", length=0.02, unit="seconds"),
        Step(kind="move", label="b", x_um=20.0, settle_s=0.0),
        Step(kind="record", label="wb", length=0.02, unit="seconds"),
    ]
    multi_group_groups = [Group(start=0, end=1, repeats=2),
                          Group(start=2, end=3, repeats=2)]
    n2 = run_to_done(list(multi_group_steps), multi_group_groups, "per_group")
    r.check(n2 == 2,
            f"per_group: repeats of the same group share a file, only "
            f"moving to the other one rolls ({n2})")

    n3 = run_to_done(list(multi_group_steps), multi_group_groups, "single")
    r.check(n3 == 1,
            f"control: single mode never rolls, over the identical protocol "
            f"({n3})")

    adapter._show_save_location()
    shown = panel._lbl_saved.toolTip()
    r.check(shown.startswith("Saves under: ") and str(out) in shown
            and "roll" in shown and panel._lbl_saved.isVisibleTo(panel.view),
            f"the Run box says where files are saved ({shown!r})")

    win.close()
    pump(app, 0.2)


def check_step_edits_keep_groups(r: Report, app) -> None:
    """Duplicate/Remove keep every group on the steps it repeated; the
    table never leaves a fraction of a frame, nor fails on a FOV-named Move
    with an axis left alone; a failed template delete is said, not raised."""
    from acqApp.routines import panel as panel_mod
    from acqApp.routines.panel import SettingsPanel
    from acqApp.routines.table import FIELDS, VALUE

    routine = Routine(steps=[Step(kind="wait", label=c) for c in "ABCDE"],
                      groups=[Group(start=1, end=2), Group(start=3, end=4)])
    panel = SettingsPanel(routine)

    def spans():
        return [(g.start, g.end) for g in routine.groups]

    panel._tbl.select_row(1)
    panel._dup_step()                           # B, inside group 1
    r.check([s.label for s in routine.steps] == list("ABBCDE")
            and spans() == [(1, 3), (4, 5)],
            f"duplicating inside a group grows it and shifts the next "
            f"({spans()})")
    panel._tbl.select_row(0)
    panel._del_step()                           # A, before both
    r.check(spans() == [(0, 2), (3, 4)],
            f"removing a step before the groups shifts them up ({spans()})")
    panel._tbl.select_row(3)
    panel._del_step()                           # D
    panel._tbl.select_row(3)
    panel._del_step()                           # E: group 2 is now empty
    r.check([s.label for s in routine.steps] == list("BBC")
            and spans() == [(0, 2)],
            f"removing a group's steps shrinks it, then drops it ({spans()})")

    tbl = panel._tbl
    panel._cmb_unit.setCurrentText("seconds")
    routine.steps[0] = Step(kind="fov", x_um=1.0, length=2.6, unit="seconds")
    tbl.reload()
    panel._cmb_unit.setCurrentText("frames")
    r.check(routine.steps[0].unit == "frames" and routine.steps[0].length == 3,
            f"switching the routine to frames rounds an FOV step's capture "
            f"({routine.steps[0].length!r})")

    routine.steps[1] = Step(kind="move", fov="A_fov", x_um=10.0)
    try:
        tbl.reload()
        tip = tbl.item(1, FIELDS.index("fov")).toolTip()
        r.check("A_fov" in tip and "NA" in tip,
                f"a FOV-named Move with an axis left alone renders ({tip!r})")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} rendering a FOV-named Move: {e}")

    head = tbl.verticalHeaderItem(0).text()
    r.check("⏺" in head,
            f"an FOV step records, so it carries the Record badge ({head!r})")

    said: list[str] = []
    panel.status_message.connect(said.append)
    real = panel_mod.templates.delete

    def locked(_name):
        raise PermissionError("in use")

    panel_mod.templates.delete = locked
    try:
        panel._cmb_tpl.addItem("held")
        panel._cmb_tpl.setCurrentText("held")
        panel._on_delete_template()
        r.check(said and "could not delete" in said[-1],
                f"a template delete the OS refuses is reported ({said})")
    except Exception as e:                       # noqa: BLE001 — that IS the bug
        r.check(False, f"{type(e).__name__} escaped Delete template: {e}")
    finally:
        panel_mod.templates.delete = real
    app.processEvents()


# ═══ timeline ═══════════════════════════════════════════════════════════

def check_layout(r: Report) -> None:
    routine = Routine(steps=[
        Step(kind="move", x_um=1, y_um=2),
        Step(kind="display", pattern="p.png"),
        Step(kind="wait", length=5, unit="seconds"),
        Step(kind="puff"),
        Step(kind="wait", length=100, unit="frames"),
    ])
    segs, total_w = _layout(routine, hz=30.0)
    r.check(len(segs) == 5, "one segment per step, no camera/repeats involved")
    r.check(total_w > 0, "a non-empty routine has a non-zero width")
    order = [s.kind for s in segs]
    r.check(order == ["move", "display", "wait", "puff", "wait"],
            "segments follow the step order")

    wait5s = segs[2]
    wait100f_known = segs[4]
    r.check(wait5s.duration_s == 5.0, "a seconds-unit Wait's duration is exact")
    r.check(abs(wait100f_known.duration_s - 100 / 30.0) < 1e-9,
            "a frames-unit Wait converts at the given rate, like estimate.py")
    r.check(segs[2].width > segs[3].width,
            "a longer Wait draws wider than an instant Puff")

    segs_no_hz, _ = _layout(routine, hz=None)
    r.check(segs_no_hz[4].duration_s is None,
            "a frames-unit Wait with no frame rate is UNKNOWN, not guessed")
    r.check(segs_no_hz[2].duration_s == 5.0,
            "…but a seconds-unit Wait next to it is still exact — unknown is "
            "per-segment, not all-or-nothing")


def check_group_span(r: Report) -> None:
    routine = Routine(
        steps=[Step(kind="wait", length=1, unit="seconds") for _ in range(4)],
        groups=[Group(start=1, end=2, repeats=3)],
    )
    order = play_order(routine)
    r.check(order == [0, 1, 2, 1, 2, 1, 2, 3],
            "control: the group's range repeats in place in play_order")
    spans = _group_spans(routine, order)
    r.check(spans == [(1, 6, 3)],
            "one bracket for the WHOLE repeated run (positions 1..6), "
            "labelled with the repeat count — not three brackets")


def check_recording_runs_separate(r: Report) -> None:
    """The point of the feature: repeats never draw as one merged bar."""
    routine = Routine(
        steps=[Step(kind="wait", length=1, unit="seconds"),
               Step(kind="record", length=1, unit="seconds"),
               Step(kind="wait", length=1, unit="seconds")],
        groups=[Group(start=1, end=1, repeats=3)],
    )
    order = play_order(routine)
    runs = _recording_runs(routine, order)
    r.check(len(runs) == 3,
            "a Record step in a repeated group draws as "
            "3 separate bars, one per repeat — never one merged bar")
    r.check(runs == [(1, 1, 0), (2, 2, 0), (3, 3, 0)],
            "…each bar covering exactly one repeat's position, same region "
            "(there is only one Record step, region 0)")
    starts = [a for a, _b, _c in runs]
    ends = [b for _a, b, _c in runs]
    r.check(all(a <= b for a, b in zip(starts, ends)),
            "every run is a valid (start <= end) range")
    gaps = all(runs[i][1] < runs[i + 1][0] for i in range(len(runs) - 1))
    r.check(gaps, "consecutive runs don't touch — a real gap separates them, "
                  "matching the bars drawn with a gap either side")


def check_recording_runs_not_grouped(r: Report) -> None:
    """No Group at all — adjacent Record steps are each their own run."""
    routine = Routine(
        steps=[Step(kind="record", length=1, unit="seconds") for _ in range(3)],
    )
    order = play_order(routine)
    runs = _recording_runs(routine, order)
    r.check(runs == [(0, 0, 0), (1, 1, 1), (2, 2, 2)],
            "adjacent Record steps draw as three bars, one region each")


def check_recording_runs_across_cycle(r: Report) -> None:
    """Only one cycle is drawn: `_recording_runs` sees a single
    `play_order` pass, whatever `cycles` says."""
    routine = Routine(
        steps=[Step(kind="record", length=1, unit="seconds")],
        cycles=2,
    )
    order = play_order(routine)
    runs = _recording_runs(routine, order)
    r.check(runs == [(0, 0, 0)],
            "one pass drawn = one run, regardless of routine.cycles (the "
            "engine's per-cycle split is check_recording_repeats')")


def check_widget_builds(r: Report) -> None:
    """The dialog the panel's Timeline… button opens builds, empty routine
    included."""
    isolate_user_state()
    app = qt_app()                      # assign it: an unreferenced one is GC'd
    from acqApp.routines.timeline import TimelineDialog

    empty = TimelineDialog(Routine(), None)
    r.check(empty is not None, "an empty routine builds a dialog, not a crash")

    routine = Routine(
        steps=[Step(kind="move", x_um=1, y_um=2),
              Step(kind="display", pattern="p.png"),
              Step(kind="record", length=5, unit="seconds"),
              Step(kind="puff")],
        groups=[Group(start=1, end=2, repeats=2)],
        cycles=3,
    )
    dlg = TimelineDialog(routine, hz=30.0)
    dlg.show()
    app.processEvents()
    r.check(dlg.isVisible(), "a populated routine's timeline shows")
    dlg.close()

    no_hz = TimelineDialog(routine, hz=None)
    no_hz.show()
    app.processEvents()
    r.check(no_hz.isVisible(),
            "…and still shows with no frame rate set (unknown-duration path)")
    no_hz.close()


def _part_timeline() -> int:
    r = Report("timeline")
    check_layout(r)
    check_group_span(r)
    check_recording_runs_separate(r)
    check_recording_runs_not_grouped(r)
    check_recording_runs_across_cycle(r)
    check_widget_builds(r)
    return r.finish()


# ═══ pairing ════════════════════════════════════════════════════════════

class _FakeFovPicker:
    """Stands in for FovPicker: "chooses" `fov` without a dialog."""
    fov = None

    def __init__(self, parent=None):
        pass

    def exec(self) -> int:
        return 1


class _FakeRoiPicker:
    path = None

    def __init__(self, parent=None):
        pass

    def exec(self) -> int:
        return 1


def _pick(panel, row: int, *, fov=None, roi=None) -> tuple[list, list]:
    """Pick `fov` (a SavedFov) or `roi` (a path) for `row` -> (status lines,
    settings_changed emissions)."""
    said: list = []
    emitted: list = []
    panel.status_message.connect(said.append)
    panel.settings_changed.connect(emitted.append)
    if fov is not None:
        _FakeFovPicker.fov = fov
        panel._pick_fov_for(row)
    else:
        _FakeRoiPicker.path = roi
        panel._pick_roi_for(row)
    panel.status_message.disconnect(said.append)
    panel.settings_changed.disconnect(emitted.append)
    return said, emitted


def check_fov_step_pick(r: Report, fov_a, roi_a: Path, orphan) -> None:
    """+ FOV step: the picked FOV, the general capture/wait, and the FOV's
    "_roi" partner, or one drawn now, or no light."""
    from acqApp.routines.panel import SettingsPanel

    panel = SettingsPanel(Routine(capture_length=300, capture_unit="frames",
                                  wait_s=4.0))
    steps = panel.settings.steps
    _FakeFovPicker.fov = fov_a
    panel._add_fov_step()
    s = steps[0] if steps else None
    r.check(s is not None and s.kind == "fov"
            and (s.x_um, s.y_um, s.z_um, s.fov) == (10.0, 20.0, 5.0,
                                                    "cellA_fov"),
            f"+ FOV step adds a step at the picked FOV ({s})")
    r.check(s is not None and (s.length, s.unit, s.wait_s) == (300, "frames",
                                                              4.0),
            "…capturing and waiting as the general settings say")
    r.check(s is not None and s.pattern == str(roi_a),
            f"…showing the FOV's paired ROI set ({s and s.pattern})")
    r.check(s is not None and s.trigger, "…and waiting for its trigger")

    _FakeFovPicker.fov = None
    panel._add_fov_step()
    r.check(len(steps) == 1, "control: cancelling the FOV picker adds nothing")

    drawn: list = []
    panel.draw_roi = lambda fov: drawn.append(fov.name) or roi_a
    from PyQt6.QtWidgets import QMessageBox
    real_q = QMessageBox.question
    QMessageBox.question = staticmethod(
        lambda *_a, **_k: QMessageBox.StandardButton.Yes)
    try:
        said, _ = _pick(panel, 0, fov=orphan)
        r.check(drawn == ["ghost_fov"] and steps[0].pattern == str(roi_a),
                f"an FOV with no ROI set offers to draw one, and the step "
                f"shows what was drawn ({drawn})")
        panel.draw_roi = lambda fov: None
        said, _ = _pick(panel, 0, fov=orphan)
        r.check(steps[0].pattern == "" and said and "without light" in said[0],
                f"…none drawn: the step captures unlit, and says so ({said})")
    finally:
        QMessageBox.question = real_q

    # General edits: matching steps follow, hand-edited ones keep theirs.
    panel = SettingsPanel(Routine(
        steps=[Step(kind="fov", x_um=1.0, length=100, unit="frames",
                    wait_s=2.0),
               Step(kind="fov", x_um=2.0, length=55, unit="frames",
                    wait_s=9.0),
               Step(kind="wait", length=3.0, unit="seconds")],
        capture_length=100, capture_unit="frames", wait_s=2.0))
    steps = panel.settings.steps
    panel._spn_capture.setValue(250)
    panel._spn_wait.setValue(6.0)
    panel._on_general_changed()
    r.check((steps[0].length, steps[0].wait_s) == (250, 6.0)
            and (steps[1].length, steps[1].wait_s) == (55, 9.0)
            and steps[2].length == 3.0,
            f"a general change moves steps still on the old value only "
            f"({[(x.length, x.wait_s) for x in steps]})")
    panel._cmb_unit.setCurrentText("seconds")
    r.check(all(x.unit == "seconds" for x in steps[:2])
            and steps[0].length == 250 and steps[1].length == 55,
            "a unit change moves every FOV step's unit, never its number")

    panel._add_step("wait")
    r.check(steps[-1].kind == "wait" and steps[-1].length == 6.0
            and steps[-1].unit == "seconds",
            "Other ▸ Wait lasts the general wait")


def check_fov_step_table(r: Report, roi_a: Path) -> None:
    """The table shows an FOV step's columns; Delete in ROI means no light."""
    from acqApp.routines.table import FIELDS, VALUE, StepTable

    steps = [Step(kind="fov", fov="cellA_fov", x_um=1.0, pattern=str(roi_a),
                  length=200, unit="frames", wait_s=3.0),
             Step(kind="puff")]
    tbl = StepTable(steps)
    text = {f: tbl.item(0, FIELDS.index(f)).text() for f in FIELDS}
    r.check(text["fov"] == "cellA_fov" and text["roi"] == "cellA_roi"
            and text["capture"] == "200 fr" and text["wait"] == "3 s",
            f"an FOV step's row: FOV, ROI, capture (with its unit), wait "
            f"({text})")
    r.check(all(tbl.item(1, FIELDS.index(f)).text() == "—"
                for f in ("fov", "roi", "capture", "wait", "settle_s")),
            "a Puff row has nothing to set")
    item = tbl.item(0, FIELDS.index("wait"))
    item.setData(VALUE, 7.5)
    r.check(steps[0].wait_s == 7.5, "editing Wait sets the step's own wait")
    tbl.clear_cell(0, "roi")
    r.check(steps[0].pattern == "" and
            tbl.item(0, FIELDS.index("roi")).text() == "no light",
            "clearing ROI leaves the step unlit")


def _part_pairing() -> int:
    r = Report("pairing")
    state = isolate_user_state()
    try:
        app = qt_app()                  # held: a collected one aborts widgets
        import acqApp.devices.dmd.roi_picker as roi_picker
        import acqApp.devices.stage.fov_picker as fov_picker
        from acqApp.devices.dmd import roi_store
        from acqApp.devices.dmd.roi import RectRoi, RoiSet
        from acqApp.devices.stage import fov_store

        fov_picker.FovPicker = _FakeFovPicker
        roi_picker.RoiSetPicker = _FakeRoiPicker
        rois = RoiSet([RectRoi(1.0, 2.0, 30.0, 40.0)])
        fov_a = fov_store.load(fov_store.save("cellA_fov", 10.0, 20.0, 5.0))
        roi_a = roi_store.save("cellA_roi", rois)
        roi_store.save("cellB_roi", rois)
        orphan = fov_store.load(fov_store.save("ghost_fov", 3.0, 4.0))

        check_fov_step_pick(r, fov_a, roi_a, orphan)
        check_fov_step_table(r, roi_a)
        app.processEvents()
    finally:
        shutil.rmtree(state, ignore_errors=True)
    return r.finish()


def _part_panel() -> int:
    r = Report("routine-panel")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_routine_panel_"))
    # Unisolated, this overwrites the operator's save folder and edits
    # their template library.
    state = isolate_user_state()
    try:
        sys.argv = ["main.py", "--mock"]
        app = qt_app()
        check_templates(r)
        check_panel_repaint(r, app)
        check_step_table(r, app, tmp)
        check_group_panel(r, app)
        check_step_repeats(r, app)
        check_step_edits_keep_groups(r, app)
        check_per_edge_routine_is_buildable(r, app)
        check_move_row_repaint(r)
        check_panel_tracker(r, app)
        check_app(r, app, state)
        check_file_rolling(r, app, state)
    finally:
        shutil.rmtree(state, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


PARTS = {
    "routine-panel": _part_panel,
    "timeline": _part_timeline,
    "pairing": _part_pairing,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
