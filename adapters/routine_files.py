"""The routine's files: rolling to a fresh file at run boundaries, the
edge log, *_BAD renames, and deleting files that never got a frame."""
from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer

from acqApp.routines.settings import group_region_at
from acqApp.saving.config import BAD_SUFFIX, rename_trial


# The camera whose frames and files a routine steps by.
FRAME_STREAM = "voltage_cam"

EDGE_LOG_HEADER = "edge,session_s,wall_time,fov,trial,path\n"

# The camera thread closes a rolled .dcimg a moment after the roll; Windows
# refuses to delete it until then.
JUNK_RETRY_MS = 500
JUNK_TRIES = 20


def _csv_text(rows: list[list]) -> str:
    """`rows` as CSV text, quoted where a FOV name or path holds a comma."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def delete_recording(path: Path) -> bool:
    """Remove one recording: a split folder's own files (named after it) and
    the folder if that empties it, or a lone .h5. Never anything else. False
    while a file is still held open."""
    try:
        if path.is_dir():
            for f in path.iterdir():
                if f.is_file() and f.name.startswith(path.name):
                    f.unlink()
            if not any(path.iterdir()):
                path.rmdir()
        elif path.suffix == ".h5" and path.is_file():
            path.unlink()
            folder = path.parent
            if folder.name == path.stem and not any(folder.iterdir()):
                folder.rmdir()
    except OSError:     # held open; anything else retries too (a QTimer slot)
        return False
    return True


class RoutineFiles:
    """RoutinesModule's file side; its state is set in RoutinesModule.__init__."""

    # ── the file ──
    def _prepare_recording(self, run) -> None:
        """With a .dcimg open, roll to the next recording's file and re-arm in
        the same swap: a plain re-arm would kill the recorder, and this way
        the edge's first frame lands in the new file. Raises to pause."""
        if self.win.dcimg_frames(FRAME_STREAM) is None:
            return
        self._doom_unused_prepared()
        if self.win.arm_camera_with_next_file(FRAME_STREAM) is not True:
            raise RuntimeError("the camera could not be armed with the next "
                               "file")
        if not self._roll_for(run):
            raise RuntimeError("could not open the next output file")
        self._prepared = (run.cycle, run.start_index)
        self._armed_with_file = True
        self._status("new .dcimg opened before the trigger step re-arms — a "
                     ".dcimg cannot span one")

    def _on_edge(self, t: float, run) -> None:
        """Log a trigger edge. Missed edges can't be seen live; the log is
        matched against the stim rig's afterwards (saving/bpod_match.py). The
        row waits for the file its recording opens, if one follows."""
        self._flush_pending_edge()
        self._edge_n += 1
        wall = time.strftime("%Y-%m-%dT%H:%M:%S")
        key = None if run is None else (run.cycle, run.start_index)
        self._pending_edge = (self._edge_n, t, wall, key, run)
        if run is None:
            self._flush_pending_edge()

    def _flush_pending_edge(self, run=None) -> None:
        """Write the pending edge; with `run`, as opening that run's file."""
        if self._pending_edge is None:
            return
        n, t, wall, key, edge_run = self._pending_edge
        self._pending_edge = None
        fov, trial, path = "", "", ""
        if run is not None and edge_run is not None:
            fov = self._fov_for(self._routine, run.start_index)[0]
            trial = self._trial_count.get(run.region, "")
            path = self.win.recording_path() or ""
        try:
            if self._edge_log is None:
                folder = Path(self.win.routine_folder())
                folder.mkdir(parents=True, exist_ok=True)
                self._edge_log = folder / (
                    f"routine_edges_{time.strftime('%Y%m%d_%H%M%S')}.csv")
                self._edge_log.write_text(EDGE_LOG_HEADER, encoding="utf-8")
            with open(self._edge_log, "a", encoding="utf-8") as fh:
                fh.write(_csv_text([[n, f"{t:.4f}", wall, fov, trial, path]]))
        except OSError as e:
            self._status(f"could not write the edge log ({e})")

    def _on_recording_begin(self, run) -> None:
        """One `/routine` entry per boundary. The first run just claims the
        open file; a later one needing a fresh file defers the roll to
        `_tick`. A sealed file takes no further run."""
        self._seal_after = None         # this run keeps the open file
        if self._prepared == (run.cycle, run.start_index):
            self._prepared = None       # its file was opened before the edge
            self._file_group_key = self._group_key_for(run)
        elif self._file_group_key is None:
            self._file_group_key = self._group_key_for(run)
        elif self._sealed or self._needs_roll(run):
            self._pending_roll_run = run
            return
        self._put(run, opening=True)

    def _on_recording_end(self, run) -> None:
        self._put(run, opening=False)
        if self._burst_n and not run.interrupted:
            self._seal_after = self.win.recording_path()

    def _needs_roll(self, run) -> bool:
        mode = self._routine.save_mode if self._routine is not None else "single"
        if mode == "per_repeat":
            return True
        if mode == "per_group":
            return self._group_key_for(run) != self._file_group_key
        return False

    def _group_key_for(self, run) -> tuple[int, int | None]:
        """(cycle, group-or-None): what "same file" means for per_group.

        Known gap: one Recording spanning two adjacent Groups can merge
        their runs into one, filed under the first Group. Splitting it would
        break the one-run-one-file invariant `final_metadata` relies on."""
        group = (group_region_at(self._routine, run.start_index)
                if self._routine is not None else None)
        return (run.cycle, group)

    def _roll_for(self, run) -> bool:
        """Close the current file and open the next, scoped to `run`."""
        junk = self._take_doomed()
        bad = self._bad_file()
        fov, coords = self._fov_for(self._routine, run.start_index)
        self.win.set_routine_save_context(fov, self._trial_for(run.region), coords)
        self._pending_scope = self._scope_for(run)
        self._rolling = True
        try:
            ok = self.win.roll_recording()
        finally:
            self._rolling = False
        if ok:
            self._sealed = False
            self._filed_from = len(self._engine.runs)
            self._file_group_key = self._group_key_for(run)
            self._discard(junk, bad)
        return ok

    def _doom_unused_prepared(self) -> None:
        """A file opened for an edge that never came (paused at the trigger,
        then Resume or Abort) is as empty as Start's travel-only one."""
        if self._prepared is not None:
            self._prepared, self._doomed = None, self.win.recording_path()

    def _take_doomed(self) -> Path | None:
        """A file doomed as empty, unless it caught frames after all."""
        junk, self._doomed = self._doomed, None
        if junk is not None and self.win.dcimg_frames(FRAME_STREAM):
            return None
        return junk

    def _discard(self, junk: Path | None, bad: Path | None) -> None:
        """Clean up a closed file: delete travel-only junk, mark a bad one."""
        if junk is not None:
            self._delete_junk(junk, JUNK_TRIES)
        if bad is not None:
            self._mark_bad(bad, JUNK_TRIES)

    def _bad_file(self) -> Path | None:
        """The open file, if every run it holds was interrupted."""
        eng = self._engine
        runs = eng.runs[self._filed_from:] if eng is not None else []
        if runs and all(x.interrupted for x in runs):
            return self.win.recording_path()
        return None

    def _mark_bad(self, path: Path, tries: int) -> None:
        """Rename a closed file to *_BAD, retried while DCAM still holds it;
        the edge log follows so Bpod matching still finds it."""
        stem = path.name if path.is_dir() else path.stem
        try:
            new = rename_trial(path, stem + BAD_SUFFIX)
        except PermissionError:
            if tries > 1:
                QTimer.singleShot(JUNK_RETRY_MS,
                                  lambda: self._mark_bad(path, tries - 1))
            else:
                self._status(f"could not rename {path.name} to *{BAD_SUFFIX} "
                             f"(still open); its settings record the fault")
            return
        except OSError as e:
            self._status(f"could not rename {path.name} to *{BAD_SUFFIX} ({e})")
            return
        self._repath_edge_log(path, new)
        self._status(f"{path.name}: interrupted — renamed {new.name}")

    def _repath_edge_log(self, old: Path, new: Path) -> None:
        log = self._edge_log
        if log is None:
            return
        try:
            rows = list(csv.reader(io.StringIO(log.read_text(encoding="utf-8"))))
            for row in rows:
                if row and row[-1] == str(old):
                    row[-1] = str(new)
            log.write_text(_csv_text(rows), encoding="utf-8")
        except OSError as e:
            self._status(f"could not update the edge log for {new.name} ({e})")

    def _delete_junk(self, path: Path, tries: int) -> None:
        if delete_recording(path):
            self._status(f"deleted {path.name} — opened before a trigger that "
                         f"never came, no frames")
        elif tries > 1:
            QTimer.singleShot(JUNK_RETRY_MS,
                              lambda: self._delete_junk(path, tries - 1))
        else:
            self._status(f"could not delete {path} (still open) — safe to "
                         f"delete by hand")

    def _scope_for(self, run) -> dict[str, Any]:
        cycle, group = self._group_key_for(run)
        return {"routine_file_cycle": cycle,
                "routine_file_group": -1 if group is None else group}

    def _put(self, run, *, opening: bool) -> None:
        rec = self._rec
        if rec is None:
            return
        # +(region+1) opens, -(region+1) closes; repeats are told apart by
        # `routine_runs`.
        edge = float(run.region + 1)
        rec.put("routine", edge if opening else -edge)
        self._filed += 1
        if (opening and self._pending_edge is not None
                and self._pending_edge[3] == (run.cycle, run.start_index)):
            self._flush_pending_edge(run)
