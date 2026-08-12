"""
Save-path resolution and the no-overwrite guarantee (audit #2).

The scenario this exists to prevent: the filename template is free text, so an
operator who sets it to `{subject}` gets the same path for every recording of
the day — and the HDF5 writer used to open with mode "w". The second recording
silently truncated the first, with no error and no way back.

Two independent defences, both checked here:
  1. `SaveConfig.resolve(unique=True)` never returns an occupied path.
  2. `HDF5Writer.open()` uses mode "x" and raises rather than truncating, so a
     caller that skips (1) fails loudly instead of destroying data.

Cheap and hardware-free: no QApplication, no device, ~1 s.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_save_paths.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from _harness import Report

from acqApp.acq.writer import HDF5Writer
from acqApp.saving import SaveConfig, sanitize

WHEN = datetime(2026, 8, 12, 14, 30, 5)


def check_stem(r: Report) -> None:
    """Token substitution and sanitisation — the inputs are operator free text."""
    cfg = SaveConfig(subject="m17", session="run2",
                     template="{subject}_{session}_{date}_{time}")
    r.check(cfg.stem(WHEN) == "m17_run2_20260812_143005",
            f"all four tokens substituted (got {cfg.stem(WHEN)!r})")

    # A path separator in the subject would otherwise create a directory, or
    # escape the save folder entirely.
    cfg = SaveConfig(subject=r"m17/../x", template="{subject}")
    r.check("/" not in cfg.stem(WHEN) and "\\" not in cfg.stem(WHEN),
            f"path separators sanitised out of the subject "
            f"(got {cfg.stem(WHEN)!r})")
    r.check(sanitize("") == "session" and sanitize("  ..  ") == "session",
            "empty/degenerate names fall back rather than producing ''")

    # An unfilled token must not leave a dangling separator.
    cfg = SaveConfig(subject="m17", session="", template="{subject}_{session}")
    r.check(cfg.stem(WHEN) == "m17", f"empty token tidied (got {cfg.stem(WHEN)!r})")


def check_unique(r: Report, tmp: Path) -> None:
    """resolve(unique=True) must never name a file that already exists."""
    for subfolder in (True, False):
        base = tmp / f"sub{int(subfolder)}"
        cfg = SaveConfig(folder=str(base), subject="m17",
                         template="{subject}", subfolder=subfolder)
        label = "subfolder" if subfolder else "flat"

        first = cfg.resolve(WHEN, unique=True)
        r.check(first.name == "m17.h5", f"[{label}] first recording keeps the stem")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"first recording")

        second = cfg.resolve(WHEN, unique=True)
        r.check(second != first and not second.exists(),
                f"[{label}] second resolves to a free path ({second.name})")
        r.check(second.name == "m17_001.h5",
                f"[{label}] suffix is _001 (got {second.name})")
        if subfolder:
            r.check(second.parent.name == "m17_001",
                    f"[{label}] the subfolder is renamed with it "
                    f"(got {second.parent.name})")
        second.parent.mkdir(parents=True, exist_ok=True)
        second.write_bytes(b"second recording")

        third = cfg.resolve(WHEN, unique=True)
        r.check(third.name == "m17_002.h5",
                f"[{label}] numbering continues (got {third.name})")
        r.check(first.read_bytes() == b"first recording",
                f"[{label}] the original file was never touched")

        # unique=False is still the plain path — the preview and the metadata
        # both rely on that staying deterministic.
        r.check(cfg.resolve(WHEN) == first,
                f"[{label}] unique=False is unchanged")


def check_writer_refuses(r: Report, tmp: Path) -> None:
    """The backstop: opening onto an existing file raises, never truncates."""
    path = tmp / "existing" / "session.h5"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not an hdf5 file, but it is somebody's data")
    size = path.stat().st_size

    w = HDF5Writer()
    try:
        w.open(path, {"subject": "m17"})
    except FileExistsError:
        r.check(True, "HDF5Writer.open refuses an existing path")
    except Exception as e:                      # noqa: BLE001 - report, don't hide
        r.check(False, f"expected FileExistsError, got {type(e).__name__}: {e}")
    else:
        w.close()
        r.check(False, "HDF5Writer.open OVERWROTE an existing file")
    r.check(path.stat().st_size == size, "the existing file is byte-for-byte intact")

    # A fresh path still works, and the deliberate-clobber escape hatch works.
    fresh = tmp / "existing" / "session_001.h5"
    w = HDF5Writer()
    w.open(fresh, {"subject": "m17"})
    w.write("wheel", 0.0, 1.5)
    w.close()
    r.check(fresh.is_file() and fresh.stat().st_size > 0,
            "a free path still records normally")

    w = HDF5Writer(overwrite=True)
    w.open(fresh, {"subject": "m17"})
    w.close()
    r.check(fresh.is_file(), "overwrite=True still truncates when asked for")


def main() -> int:
    r = Report("save-paths")
    tmp = Path(tempfile.mkdtemp(prefix="acqapp_savepaths_"))
    try:
        check_stem(r)
        check_unique(r, tmp)
        check_writer_refuses(r, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
