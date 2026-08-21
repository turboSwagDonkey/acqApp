"""Mark the pupil edge by hand, then score the tracker against it.

    # click the pupil edge on a few frames, save the answers
    acqApp\\.venv\\Scripts\\python.exe acqApp\\devices\\pupil_cam\\_mark_truth.py mark ^
        --clip "E:\\pAce\\...\\FOV1_T1_Pupil.avi" --out truth.json

    # then score whatever the tracker does against them
    ...\\_mark_truth.py score --clip "...avi" --truth truth.json

Why this exists: everything measured so far is a *proxy*. Frame-to-frame
jitter says how steady a fit is and nothing about whether it is right, and two
attempts at an automatic boundary metric both failed — first-bright-crossing
fires on the corneal glint (~r 10), last-dark-sample runs out into dark fur.
A handful of frames marked by eye settles it.

Marking: **click around the pupil edge** (5+ points), and a robust circle is
fitted to them as you go. Keys — Enter/Space accept and advance, Backspace undo
the last point, R restart the frame, S skip it, Q save and quit. Nothing here
opens a device.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _load_frames(clip: str, idx: list[int] | None):
    from acqApp.devices.pupil_cam.avi import AviReader
    rd = AviReader(clip)
    n = len(rd)
    if idx is None:
        # Spread over the clip rather than the first N: a still opening and a
        # moving end are different problems, and both should be in the answers.
        idx = list(range(0, n, max(1, n // 12)))[:12]
    idx = [i for i in idx if 0 <= i < n]
    return rd, idx


def _fit(points: list[tuple[float, float]]):
    """Robust circle through the clicked points, or None below 3."""
    if len(points) < 3:
        return None
    from acqApp.devices.pupil_cam.fits import fit_circle_robust
    p = np.asarray(points, dtype=float)
    rob = fit_circle_robust(p[:, 0], p[:, 1], sigma_k=3.0,
                            min_points=min(3, len(p)))
    if rob is None:
        return None
    return float(rob[0]), float(rob[1]), float(rob[2])


# ══════════════════════════════════════════════════════════════════════════════
#  mark
# ══════════════════════════════════════════════════════════════════════════════

def cmd_mark(args) -> int:
    import pyqtgraph as pg
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (QApplication, QLabel, QVBoxLayout, QWidget)

    from acqApp.style import apply_theme

    rd, idx = _load_frames(args.clip, args.frames)
    out = Path(args.out)
    truth: dict = {}
    if out.is_file() and not args.overwrite:
        truth = json.loads(out.read_text(encoding="utf-8"))
        print(f"{out.name}: {len(truth)} frame(s) already marked "
              f"(pass --overwrite to start again)")

    app = QApplication([])          # assign it: an unreferenced one is GC'd
    apply_theme(app, "dark")

    win = QWidget()
    win.setWindowTitle("Mark the pupil edge")
    win.resize(1150, 850)
    lay = QVBoxLayout(win)
    gv = pg.GraphicsLayoutWidget()
    vb = gv.addViewBox(lockAspect=True, invertY=True)
    img = pg.ImageItem(axisOrder="row-major")
    vb.addItem(img)
    pts = pg.ScatterPlotItem(size=9, pen=pg.mkPen("k"), brush=pg.mkBrush("y"))
    ring = pg.PlotCurveItem(pen=pg.mkPen("lime", width=2))
    vb.addItem(pts)
    vb.addItem(ring)
    lay.addWidget(gv, 1)
    info = QLabel()
    info.setWordWrap(True)
    lay.addWidget(info)

    state = {"k": 0, "points": []}
    theta = np.linspace(0, 2 * np.pi, 120)

    def show() -> None:
        i = idx[state["k"]]
        img.setImage(np.ascontiguousarray(rd.luma(i)), autoLevels=True)
        vb.autoRange()
        state["points"] = [tuple(p) for p in
                           truth.get(str(i), {}).get("points", [])]
        redraw()

    def redraw() -> None:
        i = idx[state["k"]]
        p = state["points"]
        pts.setData([q[0] for q in p], [q[1] for q in p])
        c = _fit(p)
        if c is None:
            ring.setData([], [])
        else:
            ring.setData(c[0] + c[2] * np.cos(theta), c[1] + c[2] * np.sin(theta))
        info.setText(
            f"frame {i}  ({state['k'] + 1} of {len(idx)})   "
            f"{len(p)} point(s)   "
            + (f"fit: ({c[0]:.1f}, {c[1]:.1f})  r={c[2]:.1f} px" if c
               else "click 3+ points on the pupil edge")
            + f"   |   marked so far: {len(truth)}"
            + "\nEnter/Space accept · Backspace undo · R restart · S skip · "
              "Q save and quit")

    def on_click(ev) -> None:
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        q = vb.mapSceneToView(ev.scenePos())
        state["points"].append((float(q.x()), float(q.y())))
        redraw()

    vb.scene().sigMouseClicked.connect(on_click)

    def save() -> None:
        out.write_text(json.dumps(truth, indent=2), encoding="utf-8")
        print(f"saved {len(truth)} marked frame(s) -> {out}")

    def advance(store: bool) -> None:
        i = idx[state["k"]]
        if store:
            c = _fit(state["points"])
            if c is None:
                print(f"frame {i}: need 3+ points; not stored")
                return
            truth[str(i)] = {"points": state["points"],
                             "circle": [c[0], c[1], c[2]]}
        if state["k"] + 1 >= len(idx):
            save()
            app.quit()
            return
        state["k"] += 1
        show()

    def key(ev) -> None:
        k = ev.key()
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            advance(True)
        elif k == Qt.Key.Key_Backspace:
            if state["points"]:
                state["points"].pop()
                redraw()
        elif k == Qt.Key.Key_R:
            state["points"] = []
            redraw()
        elif k == Qt.Key.Key_S:
            advance(False)
        elif k in (Qt.Key.Key_Q, Qt.Key.Key_Escape):
            save()
            app.quit()

    win.keyPressEvent = key
    show()
    win.show()
    print(f"{len(idx)} frame(s) to mark: {idx}")
    app.exec()
    return 0


# ══════════════════════════════════════════════════════════════════════════════
#  score
# ══════════════════════════════════════════════════════════════════════════════

def cmd_score(args) -> int:
    from acqApp.devices.pupil_cam.settings import PupilSettings
    from acqApp.devices.pupil_cam.track_worker import track_params
    from acqApp.devices.pupil_cam.tracking import PupilTracker

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    if not truth:
        print("no marked frames in that file")
        return 2
    marked = sorted(int(k) for k in truth)
    rd, _ = _load_frames(args.clip, None)
    frames = [np.ascontiguousarray(rd.luma(i)) for i in range(len(rd))]

    kw: dict = {}
    if args.limit:
        x, y, r = (float(v) for v in args.limit.split(","))
        kw.update(limit_x=x, limit_y=y, limit_r=r)
    if args.exclude:
        v = [float(t) for t in args.exclude.split(",")]
        kw["exclude_deg"] = tuple(zip(v[0::2], v[1::2]))

    trk = PupilTracker(**track_params(PupilSettings(**kw)))
    # Run the whole clip so the tracker is in the state it would really be in
    # at each marked frame — scoring a fresh tracker on frame 120 would measure
    # something the operator never sees.
    res = [trk.process(f) for f in frames]

    print(f"{'frame':>6s} {'truth (cx, cy, r)':>26s} {'tracker':>26s} "
          f"{'d centre':>9s} {'d r':>7s}")
    dc, dr, miss = [], [], 0
    for i in marked:
        tc = truth[str(i)]["circle"]
        x = res[i]
        if not x.found:
            miss += 1
            print(f"{i:6d} {tc[0]:8.1f} {tc[1]:8.1f} {tc[2]:7.1f}"
                  f"{'LOST':>26s}")
            continue
        d = float(np.hypot(x.center_x - tc[0], x.center_y - tc[1]))
        e = float(x.radius - tc[2])
        dc.append(d)
        dr.append(abs(e))
        print(f"{i:6d} {tc[0]:8.1f} {tc[1]:8.1f} {tc[2]:7.1f}"
              f" {x.center_x:8.1f} {x.center_y:8.1f} {x.radius:7.1f}"
              f" {d:9.2f} {e:+7.2f}")
    if dc:
        print(f"\ncentre error  mean {np.mean(dc):5.2f} px  max {np.max(dc):5.2f}")
        print(f"radius error  mean {np.mean(dr):5.2f} px  max {np.max(dr):5.2f}")
    print(f"lost on {miss} of {len(marked)} marked frame(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mark", help="click the pupil edge on a few frames")
    m.add_argument("--clip", required=True)
    m.add_argument("--out", default="pupil_truth.json")
    m.add_argument("--frames", type=lambda s: [int(v) for v in s.split(",")],
                   help="frame indices; default is 12 spread over the clip")
    m.add_argument("--overwrite", action="store_true")
    m.set_defaults(func=cmd_mark)

    s = sub.add_parser("score", help="score the tracker against marked frames")
    s.add_argument("--clip", required=True)
    s.add_argument("--truth", default="pupil_truth.json")
    s.add_argument("--limit", help="eye region as cx,cy,r")
    s.add_argument("--exclude", help="ignored sectors as lo,hi[,lo,hi...]")
    s.set_defaults(func=cmd_score)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from acqApp.console import enable_safe_console
    enable_safe_console()       # before the first print (see acqApp/console.py)
    raise SystemExit(main())
