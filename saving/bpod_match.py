"""Match a routine's edge log to Bpod's trials; void and renumber folders.

The trigger line only reaches the camera, so a missed edge is invisible live.
Bpod logs every trial start; the routine logs every edge it saw. Trial
lengths vary with outcome, so the gap pattern pins the alignment down.

    python -m acqApp.saving.bpod_match EDGES.csv SESSION.mat [--apply]

Dry run by default: prints the plan. --apply renames trial folders to Bpod's
trial numbers, writes a FOV<fov>_T<n>_VOID folder (with void.json) for each
missed trial, and logs every rename to renumber_log.csv for undoing.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from acqApp.saving.config import rename_trial, routine_stem

TOL_S = 0.5            # far below the ~6.7 s shortest trial
AMBIGUOUS_S = 0.05     # a runner-up alignment this close in rms is refused


def load_edges(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["session_s"] = float(r["session_s"])
    return rows


def load_bpod_triggers(path: Path) -> np.ndarray:
    """Each trial's camera-trigger time on Bpod's clock: TrialStartTimestamp,
    plus the CamTrigger state's onset when the file has it. Reads Bpod's own
    session file (SessionData), not a saved BpodSystem object."""
    try:
        from scipy.io import loadmat
        sd = loadmat(str(path), squeeze_me=True,
                     struct_as_record=False)["SessionData"]
        starts = np.atleast_1d(np.asarray(sd.TrialStartTimestamp, float))
        onset = np.zeros_like(starts)
        try:
            trials = np.atleast_1d(sd.RawEvents.Trial)
            for i, tr in enumerate(trials[:len(starts)]):
                cam = getattr(tr.States, "CamTrigger", None)
                if cam is not None and np.isfinite(np.atleast_1d(cam)[0]):
                    onset[i] = float(np.atleast_1d(cam)[0])
        except AttributeError:
            pass
        return starts + onset
    except NotImplementedError:          # a v7.3 (HDF5) file
        import h5py
        with h5py.File(path, "r") as f:
            return np.asarray(f["SessionData"]["TrialStartTimestamp"],
                              float).ravel()


def _pair(cam: np.ndarray, bpod: np.ndarray, a: float, b: float,
          tol: float) -> dict[int, int]:
    """cam index -> bpod index for edges within `tol` of bpod = a*cam + b."""
    target = a * cam + b
    idx = np.clip(np.searchsorted(bpod, target), 1, len(bpod) - 1)
    near = np.where(np.abs(bpod[idx - 1] - target) <= np.abs(bpod[idx] - target),
                    idx - 1, idx)
    ok = np.abs(bpod[near] - target) <= tol
    out, used = {}, set()
    for j in np.flatnonzero(ok):
        i = int(near[j])
        if i not in used:
            out[int(j)] = i
            used.add(i)
    return out


def _fit(cam, bpod, pairs):
    j = np.fromiter(pairs.keys(), int)
    i = np.fromiter(pairs.values(), int)
    if len(j) >= 2:
        a, b = np.polyfit(cam[j], bpod[i], 1)
    else:
        a, b = 1.0, float(bpod[i[0]] - cam[j[0]])
    res = bpod[i] - (a * cam[j] + b)
    return a, b, float(np.sqrt(np.mean(res ** 2)))


@dataclass
class Match:
    pairs: dict[int, int]        # edge index -> bpod trial index (0-based)
    rms_s: float
    drift_ppm: float
    unmatched_edges: list[int] = field(default_factory=list)
    problem: str = ""


def match(cam_t, bpod_t, tol: float = TOL_S) -> Match:
    """Align edge times (session clock) to Bpod trigger times (Bpod clock):
    offset from each plausible first pairing, refined by a linear fit."""
    cam = np.asarray(cam_t, float)
    bpod = np.asarray(bpod_t, float)
    if len(cam) == 0 or len(bpod) < 2:
        return Match({}, 0.0, 0.0, list(range(len(cam))), "nothing to match")
    results = []
    for j0 in range(min(3, len(cam))):          # a spurious first edge
        for k in range(len(bpod)):
            pairs = _pair(cam, bpod, 1.0, bpod[k] - cam[j0], tol)
            if len(pairs) < 2:
                continue
            a, b, _ = _fit(cam, bpod, pairs)
            pairs = _pair(cam, bpod, a, b, tol)
            a, b, rms = _fit(cam, bpod, pairs)
            results.append((len(pairs), rms, a, pairs))
    if not results:
        return Match({}, 0.0, 0.0, list(range(len(cam))), "no alignment found")
    results.sort(key=lambda r: (-r[0], r[1]))
    n, rms, a, pairs = results[0]
    m = Match(pairs, rms, (a - 1.0) * 1e6,
              [j for j in range(len(cam)) if j not in pairs])
    rivals = [r for r in results[1:] if r[3] != pairs]
    if rivals and rivals[0][0] == n and rivals[0][1] - rms < AMBIGUOUS_S:
        m.problem = "two alignments fit equally well; refusing to guess"
    elif m.unmatched_edges:
        m.problem = (f"{len(m.unmatched_edges)} camera edge(s) match no Bpod "
                     f"trial (edges {[j + 1 for j in m.unmatched_edges]})")
    return m


@dataclass
class Plan:
    renames: list[tuple[Path, str]]     # (current path, Bpod-numbered stem)
    voids: list[tuple[Path, int, dict]]  # (VOID folder, trial, note)


def plan(rows: list[dict], m: Match, n_trials: int,
         first_trial: int = 1) -> Plan:
    """Rename each edge's file to its Bpod trial number; void every Bpod
    trial from `first_trial` to the last matched one that has no edge."""
    by_trial = {i + 1: rows[j] for j, i in m.pairs.items()}
    renames, voids = [], []
    for trial, row in sorted(by_trial.items()):
        if row.get("path"):
            p = Path(row["path"])
            want = routine_stem(row["fov"], trial)
            if _stem(p) != want:
                renames.append((p, want))
    placed = [(t, r) for t, r in sorted(by_trial.items()) if r.get("path")]
    last = max(by_trial) if by_trial else 0
    for trial in range(first_trial, last + 1):
        if trial in by_trial or not placed:
            continue
        near = min(placed, key=lambda tr: abs(tr[0] - trial))[1]
        folder = _parent(Path(near["path"]))
        voids.append((folder / f"{routine_stem(near['fov'], trial)}_VOID",
                      trial, {"reason": "missed trigger", "bpod_trial": trial,
                              "fov": near["fov"]}))
    return Plan(renames, voids)


def _stem(p: Path) -> str:
    return p.stem if p.suffix == ".h5" else p.name


def _parent(p: Path) -> Path:
    """The folder a trial's file/folder sits in, beside its siblings."""
    if p.suffix == ".h5" and p.parent.name == p.stem:
        return p.parent.parent
    return p.parent


def apply(pl: Plan, log_dir: Path) -> list[tuple[str, str]]:
    """Two-phase rename (every source to a temporary name first, so T19->T20
    can't collide with the old T20), then the VOID folders. Logged. If a
    first-phase rename fails (a file still open), the ones already moved are
    put back and the error re-raised: nothing is left half-renumbered."""
    done: list[tuple[str, str]] = []
    staged = []
    try:
        for path, stem in pl.renames:
            tmp = rename_trial(path, f"{stem}__renumbering")
            staged.append((path, tmp, stem))
    except OSError as e:
        for path, tmp, _stem_ in reversed(staged):
            rename_trial(tmp, _stem(path))
        raise ApplyError(f"{e}; nothing was changed") from e
    log = log_dir / "renumber_log.csv"
    new_log = not log.exists()
    # Past here, each change is logged as it lands, so a failure leaves a
    # record of exactly what moved.
    with open(log, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_log:
            w.writerow(["old", "new"])
        try:
            for path, tmp, stem in staged:
                new = rename_trial(tmp, stem)
                done.append((str(path), str(new)))
                w.writerow(done[-1])
            for folder, _trial, note in pl.voids:
                if not folder.exists():
                    folder.mkdir(parents=True)
                    (folder / "void.json").write_text(
                        json.dumps(note, indent=2), encoding="utf-8")
                done.append(("", str(folder)))
                w.writerow(done[-1])
        except OSError as e:
            raise ApplyError(f"{e}; stopped part-way — {log.name} lists what "
                             f"changed, and folders named *__renumbering "
                             f"still need their final name") from e
    return done


class ApplyError(OSError):
    """Apply stopped; the message says whether anything changed."""


def check(edges: Path, bpod_file: Path, first_trial: int = 1,
          tol: float = TOL_S) -> tuple[list[str], Plan | None]:
    """-> (report lines, plan). The plan is None when the match is refused,
    and empty when nothing needs changing."""
    rows = load_edges(edges)
    bpod = load_bpod_triggers(bpod_file)
    m = match([r["session_s"] for r in rows], bpod, tol)
    lines = [f"{len(bpod)} Bpod trials, {len(rows)} camera edges, "
             f"{len(m.pairs)} matched; residual {m.rms_s * 1e3:.1f} ms rms, "
             f"clock drift {m.drift_ppm:+.0f} ppm"]
    if m.problem:
        lines.append(f"NOT APPLYING: {m.problem}")
        return lines, None
    pl = plan(rows, m, len(bpod), first_trial)
    lines += [f"  rename  {_stem(p)} -> {stem}" for p, stem in pl.renames]
    lines += [f"  void    trial {t}: {f.name}" for f, t, _n in pl.voids]
    if not pl.renames and not pl.voids:
        lines.append("nothing to change: every trial has its edge, numbered right")
    return lines, pl


def main(argv=None) -> int:
    from acqApp.console import enable_safe_console
    enable_safe_console()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("edges", type=Path)
    ap.add_argument("bpod", type=Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--first-trial", type=int, default=1,
                    help="first Bpod trial the routine was imaging (default 1)")
    ap.add_argument("--tol", type=float, default=TOL_S)
    args = ap.parse_args(argv)

    lines, pl = check(args.edges, args.bpod, args.first_trial, args.tol)
    print("\n".join(lines))
    if pl is None:
        return 1
    if not pl.renames and not pl.voids:
        return 0
    if not args.apply:
        print("dry run; add --apply to make these changes")
        return 0
    apply(pl, args.edges.parent)
    print(f"done; logged in {args.edges.parent / 'renumber_log.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
