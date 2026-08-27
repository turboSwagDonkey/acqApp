"""Saved routines, as files. No Qt.

A protocol worth running twice is worth keeping. One JSON file per template in
`routine_templates/`, named after the routine — a folder the operator can copy
to the rig machine, not a blob inside `acqapp_local.json`.

`Routine.from_dict` already drops anything that no longer fits, so a stale
template loads as much of itself as still makes sense and `validate()` refuses
the rest at the Start button.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from acqApp.routines.settings import Routine

SUFFIX = ".routine.json"

# Beside the package, like acqapp_local.json. Module-level so the test harness
# can redirect it — an unisolated run would write into the operator's library.
DIR = Path(__file__).resolve().parents[1] / "routine_templates"

_BAD = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """A routine name as a filename. Empty or all-punctuation -> "routine"."""
    out = _BAD.sub("_", (name or "").strip()).strip(" .")
    return out[:64] or "routine"


def path_for(name: str) -> Path:
    return DIR / f"{safe_name(name)}{SUFFIX}"


def names() -> list[str]:
    """Every saved template, alphabetically, case-insensitively."""
    try:
        found = [p.name[:-len(SUFFIX)] for p in DIR.iterdir()
                 if p.name.endswith(SUFFIX) and p.is_file()]
    except OSError:
        return []
    return sorted(found, key=str.lower)


def save(routine: Routine, name: str = "") -> Path:
    """Write `routine` as a template. Atomic, as config.save_config is."""
    dest = path_for(name or routine.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(routine.to_dict(), fh, indent=2)
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return dest


def load(name: str) -> Routine:
    """Read one back. A missing or damaged file raises — the panel says so."""
    with open(path_for(name), "r", encoding="utf-8") as fh:
        return Routine.from_dict(json.load(fh))


def delete(name: str) -> None:
    try:
        path_for(name).unlink()
    except FileNotFoundError:
        pass
