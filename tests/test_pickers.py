"""`RoiSetPicker` and `FovPicker`, the two `widgets.SessionPicker`s. A break
here is invisible until an operator presses "Load" or "Go to FOV…" mid-run.

Control: an empty list is disabled rather than holding a pickable placeholder
row, so a stray Ok cannot return the placeholder string as a path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _harness import Report, isolate_user_state, qt_app, run_parts

isolate_user_state()
app = qt_app()  # held: a collected QApplication aborts widget construction

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


def _write(path: Path, **payload) -> Path:
    """A store file written by hand: a chosen folder and saved_at."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _part_pairs() -> int:
    """`routines.pairs`: "<base>_fov" <-> "<base>_roi", by typed name."""
    from acqApp.routines import pairs

    r = Report("pairs")
    r.check([pairs.base_name(n) for n in
             ("cell A_fov", "cell A_roi", "cell A", " x_FOV ")]
            == ["cell A", "cell A", "cell A", "x"],
            "base_name strips one _fov/_roi suffix, any case, and whitespace")
    r.check(pairs.fov_name("x_roi") == "x_fov"
            and pairs.roi_name("x_fov") == "x_roi"
            and pairs.roi_name("x") == "x_roi",
            "fov_name/roi_name swap or add the suffix")

    rois = RoiSet([RectRoi(1.0, 2.0, 30.0, 40.0)])

    # ── by the typed name, not the sanitised stem ──
    fov = fov_store.load(fov_store.save("L2/3_fov", 1.0, 2.0))
    roi = roi_store.save("L2/3_roi", rois)
    r.check(pairs.roi_for_fov(fov) == roi,
            f"a FOV finds its ROI set though both stems were sanitised "
            f"({roi.name})")
    back = pairs.fov_for_roi(roi)
    r.check(back is not None and back.path == fov.path,
            "…and the ROI set finds the FOV back")

    # ── controls: no suffix, no partner, no file ──
    plain_fov = fov_store.load(fov_store.save("plain", 1.0, 2.0))
    plain_roi = roi_store.save("plain", rois)
    r.check(pairs.roi_for_fov(plain_fov) is None
            and pairs.fov_for_roi(plain_roi) is None,
            "control: names without a suffix pair with nothing, even the same "
            "name")
    ghost = fov_store.load(fov_store.save("ghost_fov", 1.0, 2.0))
    r.check(pairs.roi_for_fov(ghost) is None,
            "control: a _fov with no saved _roi pairs with nothing")
    r.check(pairs.fov_for_roi(roi_store.SESSION_DIR / "missing.roi.json")
            is None, "control: a missing ROI file pairs with nothing")
    wrong = roi_store.save("ghost_fov", rois)
    r.check(pairs.fov_for_roi(wrong) is None,
            "control: an ROI set named _fov is not anyone's _roi")

    # ── several candidates: newest wins ──
    old = _write(roi_store.SESSION_DIR / "cellC_old.roi.json",
                 name="cellC_roi", saved_at="2020-01-01T00:00:00", rois=[])
    new = _write(roi_store.SESSION_DIR / "cellC_new.roi.json",
                 name="cellC_roi", saved_at="2026-01-01T00:00:00", rois=[])
    _write(roi_store.SESSION_DIR / "cellC_mid.roi.json",
           name="cellC_roi", saved_at="2024-01-01T00:00:00", rois=[])
    fov_c = fov_store.load(fov_store.save("cellC_fov", 5.0, 6.0))
    r.check(pairs.roi_for_fov(fov_c) == new,
            f"the newest same-name ROI set wins ({pairs.roi_for_fov(fov_c)})")
    _write(fov_store.SESSION_DIR / "cellC_a.fov.json", name="cellC_fov",
           saved_at="2019-01-01T00:00:00", x_um=0.0, y_um=0.0)
    back = pairs.fov_for_roi(old)
    r.check(back is not None and back.path == fov_c.path,
            "…and the newest same-name FOV, from any of the ROI's twins")

    # ── several folders: the picked file's own folder first ──
    near = roi_store.SESSION_DIR.parents[1] / "elsewhere"
    near_fov = fov_store.load(_write(
        near / "cellD.fov.json", name="cellD_fov",
        saved_at="2020-01-01T00:00:00", x_um=7.0, y_um=8.0))
    near_roi = _write(near / "cellD.roi.json", name="cellD_roi",
                      saved_at="2020-01-01T00:00:00", rois=[])
    sess_roi = roi_store.save("cellD_roi", rois)
    sess_fov = fov_store.load(fov_store.save("cellD_fov", 9.0, 9.0))
    r.check(pairs.roi_for_fov(near_fov) == near_roi,
            "a FOV's partner is looked for beside it first, though a newer "
            "one is in the session")
    back = pairs.fov_for_roi(near_roi)
    r.check(back is not None and back.path == near_fov.path,
            "…and the same the other way")
    r.check(pairs.roi_for_fov(sess_fov) == sess_roi,
            "control: a session FOV's partner is the session one")
    only_arch = _write(roi_store.ARCHIVE_DIR / "cellE.roi.json",
                       name="cellE_roi", saved_at="2020-01-01T00:00:00",
                       rois=[])
    fov_e = fov_store.load(fov_store.save("cellE_fov", 1.0, 1.0))
    r.check(pairs.roi_for_fov(fov_e) == only_arch,
            "with nothing nearer, the archive is still searched")

    # ── archive pairs with archive (the two stores live in different roots) ──
    arch_fov = fov_store.load(_write(
        fov_store.ARCHIVE_DIR / "cellF.fov.json", name="cellF_fov",
        saved_at="2020-01-01T00:00:00", x_um=1.0, y_um=1.0))
    arch_roi = _write(roi_store.ARCHIVE_DIR / "cellF.roi.json",
                      name="cellF_roi", saved_at="2020-01-01T00:00:00",
                      rois=[])
    roi_store.save("cellF_roi", rois)               # newer, in the session
    fov_store.save("cellF_fov", 2.0, 2.0)
    r.check(pairs.roi_for_fov(arch_fov) == arch_roi,
            f"an archived FOV pairs with the archived ROI set, not a newer "
            f"session one ({pairs.roi_for_fov(arch_fov)})")
    back = pairs.fov_for_roi(arch_roi)
    r.check(back is not None and back.path == arch_fov.path,
            "…and the same the other way")
    return r.finish()


PARTS = {
    "pickers": main,
    "pairs": _part_pairs,
}


if __name__ == "__main__":
    sys.exit(run_parts(PARTS))
