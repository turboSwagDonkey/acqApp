"""The two saved-file pickers: `RoiSetPicker` and `FovPicker`.

Both are `widgets.SessionPicker` subclasses — one dialog shape, differing only
in what a row says and what the choice yields. Nothing built either of them
before, which is exactly why the shared base needs a test: a break here is
invisible until an operator presses "Load" or "Go to FOV…" mid-experiment.

The control that matters is the EMPTY case. An empty list is disabled rather
than holding a pickable "(none saved this session)" row, so a stray Ok cannot
return a path that is really a placeholder string.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_pickers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from _harness import Report, isolate_user_state, qt_app

isolate_user_state()
app = qt_app()          # must be held: an unreferenced QApplication is
                        # collected and widget construction aborts natively

from acqApp.devices.dmd import roi_store                      # noqa: E402
from acqApp.devices.dmd.roi import RectRoi, RoiSet            # noqa: E402
from acqApp.devices.dmd.roi_picker import RoiSetPicker        # noqa: E402
from acqApp.devices.stage import fov_store                    # noqa: E402
from acqApp.devices.stage.fov_picker import FovPicker         # noqa: E402
from acqApp.widgets import _PATH_ROLE                         # noqa: E402


def item_path(dlg, row: int) -> Path:
    return Path(dlg._list.item(row).data(_PATH_ROLE))


def main() -> int:
    r = Report("pickers")

    # ── 1. nothing saved yet (the control) ──
    d = RoiSetPicker()
    r.check(d._list.count() == 1 and not d._list.isEnabled(),
            "an empty ROI picker shows one placeholder row, disabled")
    d._accept_selected()
    r.check(d.path is None,
            "control: Ok on an empty ROI picker chooses nothing")

    f = FovPicker()
    r.check(not f._list.isEnabled(),
            "an empty FOV picker is disabled the same way")
    f._accept_selected()
    r.check(f.fov is None and f.path is None,
            "control: Ok on an empty FOV picker chooses nothing")

    # ── 2. ROI sets ──
    rois = RoiSet([RectRoi(1.0, 2.0, 30.0, 40.0)])
    first = roi_store.save("alpha", rois)
    roi_store.save("beta", rois)

    d = RoiSetPicker()
    r.check(d._list.count() == 2 and d._list.isEnabled(),
            "both of this session's ROI sets are listed, and pickable")
    r.check("beta" in d._list.item(0).text(),
            "newest first — the set just saved is row 0")
    r.check("alpha" in d._list.item(1).text()
            and "(" in d._list.item(1).text(),
            "a row shows the set's saved name and its timestamp")
    d.chose(item_path(d, 1))
    r.check(d.path == first,
            f"choosing row 1 returns that set's own path ({d.path})")

    # ── 3. FOV bookmarks: position in the row, the record as the result ──
    fov_store.save("spot one", 10.0, 20.0, z_um=5.0, camera_preset="full")
    newest = fov_store.save("spot two", 30.0, 40.0)

    f = FovPicker()
    r.check(f._list.count() == 2, "both of this session's FOVs are listed")
    top = f._list.item(0).text()
    r.check("spot two" in top and "[30, 40]" in top,
            f"a FOV row carries its position, newest first ({top})")
    r.check("[10, 20, 5]" in f._list.item(1).text(),
            "a FOV saved with Z shows all three axes")
    f.chose(item_path(f, 0))
    r.check(f.fov is not None and f.fov.x_um == 30.0 and f.fov.y_um == 40.0,
            "choosing a FOV yields the loaded record, not just a path")
    r.check(f.path == newest,
            "…and the base class's own .path is set alongside it")

    # ── 4. the archive is reachable, and separate from the session list ──
    r.check(fov_store.ARCHIVE_DIR != fov_store.SESSION_DIR
            and not [p for p in fov_store.list_archive()
                     if p.name.startswith("spot")],
            "this session's FOVs are in the session list, not the archive")

    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
