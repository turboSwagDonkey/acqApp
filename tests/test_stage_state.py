"""
Stage: disconnected-motion guards (#6) and calibration persistence (#7).

Two failure modes, both of which cost real calibration time on the rig:

  #6 `move_to_um`/`jog_um` used `self._dev` with no connection check, so a
     motion command after a disconnect raised AttributeError — which the panel
     does not present as a stage error, unlike StageControllerError.

  #7 `save_axis_updates()` read the config file unguarded. The callers mutate
     the live axes *first*, and `establish_frame()` gets there only after
     driving both axes into their hard limits, so a missing config file threw
     the freshly measured origin away and left memory disagreeing with disk.

`config_path()` points at the real shared calibration (`stage_control/
config.json`), so every check here redirects it at a temp file first. Writing
the operator's calibration from a test would be exactly the accident the
harness docstring warns about.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_stage_state.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from _harness import Report

from acqApp.stage import settings as S
from acqApp.stage.control import StageController, StageControllerError

UPDATES = {1: {"true_center": 12345, "soft_min": -1000, "soft_max": 1000},
           2: {"true_center": 6789}}


def redirect_config(tmp: Path, name: str) -> Path:
    """Point config_path() at a temp file — never the operator's calibration."""
    path = tmp / name
    S._SHARED_CONFIG = path
    S._LOCAL_CONFIG = path
    return path


def check_guards(r: Report) -> None:
    """#6 — motion on a disconnected controller is a stage error, not a crash."""
    ctl = StageController(S.StageSettings())          # never connect()ed
    for label, call in (
        ("move_to_um", lambda: ctl.move_to_um("x", 100.0)),
        ("jog_um",     lambda: ctl.jog_um("x", 10.0)),
        ("read_xy_um", lambda: ctl.read_xy_um()),
    ):
        try:
            call()
        except StageControllerError:
            r.check(True, f"{label}() while disconnected raises StageControllerError")
        except Exception as e:                        # noqa: BLE001 - report it
            r.check(False, f"{label}() raised {type(e).__name__}: {e}")
        else:
            r.check(False, f"{label}() while disconnected did not raise")

    # stop/stop_all are called on teardown paths and must stay silent no-ops.
    try:
        ctl.stop("x")
        ctl.stop_all()
        r.check(True, "stop()/stop_all() while disconnected are no-ops")
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"stop() while disconnected raised {type(e).__name__}: {e}")


def check_persist_missing(r: Report, tmp: Path) -> None:
    """#7 — no config file yet: create one instead of raising."""
    path = redirect_config(tmp, "missing.json")
    r.check(not path.exists(), "config file absent to begin with")
    try:
        S.save_axis_updates(UPDATES)
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"save_axis_updates raised {type(e).__name__}: {e}")
        return
    r.check(path.is_file(), "a config file was created")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    by_index = {a["index"]: a for a in cfg["axes"]}
    r.check(by_index[1]["true_center"] == 12345 and by_index[2]["true_center"] == 6789,
            "both axes' calibration survived to disk")


def check_persist_merges(r: Report, tmp: Path) -> None:
    """An existing config keeps its other keys, and is backed up first."""
    path = redirect_config(tmp, "existing.json")
    original = {"port": "COM54", "margin_um": 50,
                "axes": [{"index": 1, "counts_per_um": 20.0, "slope": 1.5},
                         {"index": 2, "counts_per_um": 20.0}]}
    path.write_text(json.dumps(original), encoding="utf-8")

    S.save_axis_updates({1: {"true_center": 999}})
    cfg = json.loads(path.read_text(encoding="utf-8"))
    ax1 = {a["index"]: a for a in cfg["axes"]}[1]
    r.check(cfg.get("port") == "COM54", "unrelated top-level keys untouched")
    r.check(ax1.get("counts_per_um") == 20.0 and ax1.get("slope") == 1.5,
            "unrelated axis keys untouched")
    r.check(ax1.get("true_center") == 999, "the update landed")

    bak = path.with_suffix(path.suffix + ".bak")
    r.check(bak.is_file() and json.loads(bak.read_text(encoding="utf-8")) == original,
            "previous contents kept as .bak (one step undoable)")


def check_persist_corrupt(r: Report, tmp: Path) -> None:
    """A truncated config must not swallow a just-measured calibration."""
    path = redirect_config(tmp, "corrupt.json")
    path.write_text('{"axes": [{"index": 1,', encoding="utf-8")   # killed mid-write

    try:
        S.save_axis_updates({1: {"true_center": 42}})
    except Exception as e:                            # noqa: BLE001
        r.check(False, f"save over a corrupt config raised {type(e).__name__}: {e}")
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))
    r.check({a["index"]: a for a in cfg["axes"]}[1]["true_center"] == 42,
            "calibration written over a corrupt config")
    bak = path.with_suffix(path.suffix + ".bak")
    r.check(bak.is_file() and bak.read_text(encoding="utf-8").startswith('{"axes"'),
            "the corrupt original is preserved in .bak, not discarded")


def main() -> int:
    r = Report("stage-state")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_stage_"))
    real = S.config_path()
    before = real.stat().st_mtime_ns if real.is_file() else None
    try:
        check_guards(r)
        check_persist_missing(r, tmp)
        check_persist_merges(r, tmp)
        check_persist_corrupt(r, tmp)
        after = real.stat().st_mtime_ns if real.is_file() else None
        r.check(after == before,
                f"the operator's real calibration ({real.name}) was not written")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
