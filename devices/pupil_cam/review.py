"""Offline pupil review: track a whole recorded clip, then hand-fix frames. No Qt.

The auto fit and the hand edits are kept apart, so re-tracking with new
parameters never throws a correction away. What is reported (`fit_at`,
`table`) is the edit where there is one, else the auto fit. Smoothing is not
applied to the auto fits only (centred, so no lag), never to a hand edit.

Sidecar files sit beside the video, which is never modified:
    <clip>.pupil.json   settings, the settings the auto fits came from, frame
                        count, clip size, which frames were hand-edited
    <clip>.pupil.npz    auto / manual / blink / final / radius arrays
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Callable

import numpy as np

from acqApp.devices.pupil_cam.clip import open_clip
from acqApp.devices.pupil_cam.settings import PupilSettings
from acqApp.devices.pupil_cam.tracking import PupilTracking

FIELDS = ("x", "y", "major", "minor", "angle")
_NAN5 = (float("nan"),) * 5
# What the auto fit depends on; the rest (camera, display, LED) does not.
_TRACK_FIELDS = ("limit_x0", "limit_y0", "limit_x1", "limit_y1",
                 "track_threshold", "track_blur", "track_model",
                 "track_rim_check", "track_rim_dark", "track_whiskers",
                 "blink_detect", "blink_drop_frac", "blink_baseline_window",
                 "cr_remove", "cr_threshold", "cr_pad", "cr_ring", "cr_reach",
                 "cr_pins")


def sidecar_paths(video: str | Path) -> tuple[Path, Path]:
    v = Path(video)
    return (v.with_name(v.name + ".pupil.json"),
            v.with_name(v.name + ".pupil.npz"))


def _fit_from(row: np.ndarray):
    """A `PupilFit` from one table row, or None when the row is NaN."""
    if np.isnan(row[0]):
        return None
    from acqApp.devices.pupil_cam.eyeloop_tracker import PupilFit
    return PupilFit(*(float(v) for v in row))


def _row_from(fit) -> tuple:
    if fit is None:
        return _NAN5
    return (fit.center_x, fit.center_y, fit.semi_major, fit.semi_minor,
            fit.angle_deg)

# Fitting options added with the default on.
_ON_SINCE = ("track_whiskers",)


def _settings_from(d) -> PupilSettings | None:
    """Settings from a sidecar's dict, each value coerced to its default's
    type (a script may have written 21.0 for an int). Raises on a value that
    can't be, so the caller can set the sidecar aside."""
    if not isinstance(d, dict):
        return None
    base = PupilSettings()
    kw = {}
    for f in dataclasses.fields(PupilSettings):
        if f.name not in d:
            # Newer than the sidecar, so it wasn't applied: off, not the default.
            if f.name in _ON_SINCE:
                kw[f.name] = False
            continue
        v, default = d[f.name], getattr(base, f.name)
        if isinstance(default, bool):
            kw[f.name] = bool(v)
        elif isinstance(default, int):
            kw[f.name] = int(round(float(v)))
        elif isinstance(default, float):
            kw[f.name] = float(v)
        elif isinstance(default, str):
            kw[f.name] = str(v)
        elif f.name == "cr_pins":
            kw[f.name] = [tuple(float(x) for x in pin) for pin in v
                          if len(pin) == 3]
        else:
            kw[f.name] = v
    return PupilSettings(**kw)


def _same_name(a: str, b: str) -> bool:
    """Windows paths are case-insensitive: the same clip typed in another
    case must still find its sidecar."""
    return os.path.normcase(a) == os.path.normcase(b)


def _free_old(path: Path) -> Path:
    """`<path>.old`, or .old1, .old2... so an earlier backup is never
    overwritten."""
    cand, k = path.with_name(path.name + ".old"), 0
    while cand.exists():
        k += 1
        cand = path.with_name(f"{path.name}.old{k}")
    return cand


def same_tracking(a: PupilSettings | None, b: PupilSettings | None) -> bool:
    """True when `a` and `b` would give the same auto fits."""
    if a is None or b is None:
        return a is b
    return all(getattr(a, f) == getattr(b, f) for f in _TRACK_FIELDS)


def _write_pair(files: list[tuple[Path, Callable]]) -> None:
    """Write every file to a temporary first, then rename them all into
    place: a crash, full disk or a locked file leaves the old pair whole and
    no temporaries behind. A rename is retried briefly, since on Windows a
    virus scanner or indexer can hold a file for a moment."""
    import time
    tmps = []
    try:
        for path, write in files:
            tmp = path.with_name(path.name + ".tmp")
            tmps.append(tmp)
            with open(tmp, "wb") as f:
                write(f)
        for (path, _), tmp in zip(files, tmps):
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1)
    finally:
        for tmp in tmps:
            try:
                tmp.unlink()
            except OSError:
                pass            # already renamed into place


class PupilReview:
    """One clip, its parameters, its auto fits and its hand edits."""

    # EyeLoop's config is process-global: a clip fit and live tracking must
    # not run at once. Counted here so either side can check.
    _fitting = 0
    _fitting_lock = threading.Lock()

    def __init__(self, video: str | Path,
                 settings: PupilSettings | None = None) -> None:
        self.reader = open_clip(video)
        self.video = Path(video)
        self.settings = settings or PupilSettings()
        n = len(self.reader)
        self.auto = np.full((n, 5), np.nan)
        self.manual = np.full((n, 5), np.nan)
        self.blink = np.zeros(n, dtype=bool)
        self.tracked = False
        # The settings `auto` came from; `settings` may have moved on since.
        self.tracked_with: PupilSettings | None = None
        # Why an existing sidecar was not used, if one was not.
        self.sidecar_note: str | None = None
        self._keep_old_sidecar = False
        self.tracking = PupilTracking()
        # Earlier traces, newest last: (auto, blink, tracked_with). Kept for
        # Revert; not saved.
        self.history: list[tuple] = []
        # Before each gap fill/re-track, newest last: (first frame, old rows).
        self.edit_undo: list[tuple[int, np.ndarray]] = []
        self._preview_tracking = None
        self.last_mask = None       # from preview_fit: crop-sized bool
        self.last_box = None        # ...and where the crop sits

    def __len__(self) -> int:
        return len(self.reader)

    @classmethod
    def fitting(cls) -> bool:
        """True while any clip in this process is being tracked."""
        return cls._fitting > 0

    @property
    def stale(self) -> bool:
        """The auto fits came from different tracking settings than shown."""
        return self.tracked and not same_tracking(self.tracked_with, self.settings)

    # ── tracking ─────────────────────────────────────────────────────────────
    def track_all(self, progress: Callable[[int, int], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> bool:
        """Fit every frame in order (the tracker walks from the last frame's
        centre, so order matters). The new fits replace the old only when the
        whole clip is done: stopped (returns False) or failed (raises), the
        previous fits are kept. Hand edits are never touched."""
        # Lazy: EyeLoop is process-global and needs a clone.
        from acqApp.devices.pupil_cam.track_worker import _BlinkDetector

        settings = self.settings
        st = dataclasses.replace(settings, track=True)
        n = len(self)
        auto = np.full((n, 5), np.nan)
        blinks = np.zeros(n, dtype=bool)
        with PupilReview._fitting_lock:
            PupilReview._fitting += 1
        try:
            # A fresh tracker, so a changed box or model re-arms and a stale
            # seed from the last run cannot leak in.
            self.tracking = PupilTracking()
            blink = _BlinkDetector()
            for i in range(n):
                if should_stop is not None and should_stop():
                    return False
                fit = self.tracking.track(
                    np.ascontiguousarray(self.reader.luma(i)), st)
                if not self.tracking.available:
                    raise RuntimeError(self.tracking.error)
                auto[i] = _row_from(fit)
                if st.blink_detect:
                    blinks[i] = blink.check(
                        fit.radius if fit is not None else None,
                        st.blink_drop_frac, st.blink_baseline_window)
                if progress is not None:
                    progress(i + 1, n)
        finally:
            with PupilReview._fitting_lock:
                PupilReview._fitting -= 1
        if self.tracked:
            self.history.append((self.auto, self.blink, self.tracked_with))
        self.auto, self.blink = auto, blinks
        self.tracked = True
        self.tracked_with = settings
        return True

    def revert(self) -> bool:
        """Back to the trace before the last full track, and to the tracking
        settings that made it. False when there is none."""
        if not self.history:
            return False
        self.auto, self.blink, tw = self.history.pop()
        self.tracked_with = tw
        if tw is not None:
            self.settings = dataclasses.replace(
                self.settings, **{f: getattr(tw, f) for f in _TRACK_FIELDS})
        return True

    def preview_fit(self, i: int, settings: PupilSettings | None = None,
                    warmup: int = 2):
        """Frame `i` fitted with `settings` (default: the current ones), for
        showing a change before it is applied, and what reflection removal
        blanked there. The tracker walks from the last frame's centre and
        looks for reflections around the last fit, so a jump (`warmup` > 0)
        starts from the run's own fit before `i` when there is one — the
        state a run through the clip had there — else warms up over the
        `warmup` frames before. Raises when tracking is unavailable."""
        st = dataclasses.replace(settings or self.settings, track=True)
        with PupilReview._fitting_lock:
            PupilReview._fitting += 1
        try:
            # One tracker for previews, re-armed only when the box or shape
            # changes; a fresh one per call would re-arm EyeLoop every time.
            t = self._preview_tracking
            if not isinstance(t, PupilTracking):
                t = self._preview_tracking = PupilTracking()
            fit = None
            start = max(0, i - warmup)
            if warmup:
                done = np.flatnonzero(~np.isnan(self.auto[:i, 0]))
                if done.size:
                    t.seed((self.reader.height, self.reader.width), st,
                           _fit_from(self.auto[done[-1]]))
                    start = i
            for j in range(start, i + 1):
                fit = t.track(np.ascontiguousarray(self.reader.luma(j)), st)
                if not t.available:
                    raise RuntimeError(t.error)
            # What reflection removal blanked on frame i, for drawing.
            self.last_mask = getattr(t, "last_mask", None)
            self.last_box = getattr(t, "last_box", None)
            return fit
        finally:
            with PupilReview._fitting_lock:
                PupilReview._fitting -= 1

    # ── hand edits ───────────────────────────────────────────────────────────
    def set_manual(self, i: int, fit) -> None:
        """Pin frame `i` to `fit` (a `PupilFit`); None clears the edit."""
        self.manual[i] = _row_from(fit)

    def clear_manual(self, i: int) -> None:
        self.manual[i] = _NAN5

    def is_edited(self, i: int) -> bool:
        return not np.isnan(self.manual[i, 0])

    @property
    def edited(self) -> np.ndarray:
        return ~np.isnan(self.manual[:, 0])

    # ── many frames at once: the gap between two anchors ─────────────────────
    def gap_before(self, i: int) -> tuple[int, int] | None:
        """(a, i), `a` the nearest hand-edited frame before `i`: the anchor a
        gap is filled or re-tracked from. None with no anchor or no gap."""
        prev = np.flatnonzero(self.edited[:i])
        if not prev.size or i - int(prev[-1]) < 2:
            return None
        return int(prev[-1]), int(i)

    def interpolate(self, a: int, b: int) -> int:
        """Frames between `a` and `b` become edits eased linearly from a's
        ellipse to b's (angle the short way round, mod 180); `b` is pinned
        too, so it anchors the next gap. Returns the frames filled. Raises
        ValueError when an end has no ellipse."""
        t = self.table()
        if np.isnan(t[a, 0]) or np.isnan(t[b, 0]):
            raise ValueError("both ends need an ellipse")
        f = np.linspace(0.0, 1.0, b - a + 1)[1:-1, None]
        mid = t[a] + (t[b] - t[a]) * f
        turn = (t[b, 4] - t[a, 4] + 90.0) % 180.0 - 90.0
        mid[:, 4] = (t[a, 4] + turn * f[:, 0]) % 180.0
        self._remember_edits(a + 1, b + 1)
        self.manual[a + 1:b] = mid
        self.manual[b] = t[b]
        return b - a - 1

    def retrack_range(self, a: int, b: int,
                      progress: Callable[[int, int], None] | None = None,
                      should_stop: Callable[[], bool] | None = None) -> bool:
        """Re-fit the frames after `a` up to `b` with the current settings,
        kept as edits so a later full track leaves them alone; `b` only when
        it isn't an edit already. The tracker starts on `a`, so it walks in
        from a known pupil. A frame it can't fit keeps its old answer.
        Stopped (False) or failed (raises): nothing changes."""
        st = dataclasses.replace(self.settings, track=True)
        hi = b if self.is_edited(b) else b + 1
        out = np.full((hi - a - 1, 5), np.nan)
        with PupilReview._fitting_lock:
            PupilReview._fitting += 1
        try:
            t = PupilTracking()
            t.track(np.ascontiguousarray(self.reader.luma(a)), st)
            for k in range(a + 1, hi):
                if not t.available:
                    raise RuntimeError(t.error)
                if should_stop is not None and should_stop():
                    return False
                out[k - a - 1] = _row_from(
                    t.track(np.ascontiguousarray(self.reader.luma(k)), st))
                if progress is not None:
                    progress(k - a, hi - a - 1)
        finally:
            with PupilReview._fitting_lock:
                PupilReview._fitting -= 1
        self._remember_edits(a + 1, hi)
        got = ~np.isnan(out[:, 0])
        self.manual[a + 1:hi][got] = out[got]
        return True

    def _remember_edits(self, lo: int, hi: int) -> None:
        self.edit_undo.append((lo, self.manual[lo:hi].copy()))

    def undo_edits(self) -> bool:
        """Back to the edits before the last gap fill or re-track."""
        if not self.edit_undo:
            return False
        lo, rows = self.edit_undo.pop()
        self.manual[lo:lo + len(rows)] = rows
        return True

    # ── the answer ───────────────────────────────────────────────────────────
    def smoothed_auto(self) -> np.ndarray:
        """`auto` averaged over `smooth_window` frames centred on each, when
        Stabilize is on. A frame with no fit breaks the average, as live."""
        st = self.settings
        win = int(st.smooth_window)
        if not st.smooth or win <= 1:
            return self.auto
        half = win // 2
        out = self.auto.copy()
        ok = ~np.isnan(self.auto[:, 0])
        n = len(ok)
        i = 0
        while i < n:                        # each run of consecutive fits
            if not ok[i]:
                i += 1
                continue
            j = i
            while j < n and ok[j]:
                j += 1
            run = self.auto[i:j]
            # Angles average on the doubled circle: an ellipse at 179 deg
            # and one at 1 deg are nearly the same.
            ang = np.radians(run[:, 4] * 2.0)
            cs = np.cumsum(np.vstack([np.zeros((1, 6)), np.column_stack(
                [run[:, :4], np.cos(ang), np.sin(ang)])]), axis=0)
            for k in range(j - i):
                a, b = max(0, k - half), min(j - i, k + half + 1)
                m = (cs[b] - cs[a]) / (b - a)
                out[i + k, :4] = m[:4]
                out[i + k, 4] = (np.degrees(np.arctan2(m[5], m[4])) / 2.0) % 180.0
            i = j
        return out

    def table(self) -> np.ndarray:
        """(n, 5) x, y, major, minor, angle: the edit where there is one, else
        the (stabilized, if on) auto fit."""
        return np.where(self.edited[:, None], self.manual, self.smoothed_auto())

    def fit_at(self, i: int):
        return _fit_from(self.table()[i])

    def radius(self) -> np.ndarray:
        t = self.table()
        return (t[:, 2] + t[:, 3]) / 2.0

    def suspects(self, jump: float = 0.25, window: int = 7) -> list[int]:
        """Frames worth a look: no fit, or a radius more than `jump` (a
        fraction) off the median of its neighbours. Edited frames are
        skipped: someone already looked."""
        r = self.radius()
        n = len(r)
        out = []
        half = max(1, window // 2)
        for i in range(n):
            if self.is_edited(i):
                continue
            if np.isnan(r[i]):
                out.append(i)
                continue
            nb = np.concatenate((r[max(0, i - half):i], r[i + 1:i + 1 + half]))
            nb = nb[~np.isnan(nb)]
            if nb.size and abs(r[i] - np.median(nb)) > jump * np.median(nb):
                out.append(i)
        return out

    # ── persistence ──────────────────────────────────────────────────────────
    def _identity(self) -> dict:
        return {"video": self.video.name, "frames": len(self),
                "video_bytes": self.video.stat().st_size}

    def save(self) -> tuple[Path, Path]:
        """Raises OSError when the folder can't be written. A sidecar that
        was found but not used (another clip's) is kept as *.old first."""
        js, npz = sidecar_paths(self.video)
        if self._keep_old_sidecar:
            for p in (js, npz):
                if p.exists():
                    os.replace(p, _free_old(p))
            self._keep_old_sidecar = False
        # In both files: a pair whose ids differ was half-written.
        gen = uuid.uuid4().hex
        meta = {**self._identity(), "generation": gen, "tracked": self.tracked,
                "fields": list(FIELDS),
                "edited_frames": [int(i) for i in np.flatnonzero(self.edited)],
                "settings": dataclasses.asdict(self.settings),
                "tracked_with": (dataclasses.asdict(self.tracked_with)
                                 if self.tracked_with is not None else None)}
        _write_pair([
            (npz, lambda f: np.savez(
                f, auto=self.auto, manual=self.manual, blink=self.blink,
                final=self.table(), radius=self.radius(),
                generation=np.array(gen))),
            (js, lambda f: f.write(json.dumps(meta, indent=2).encode("utf-8"))),
        ])
        return js, npz

    @classmethod
    def load(cls, video: str | Path,
             settings: PupilSettings | None = None) -> "PupilReview":
        """Open `video`, restoring its sidecar if it is this clip's and
        readable. Otherwise the sidecar is left alone (and kept as *.old on
        the next save) and `sidecar_note` says why. `settings` is only the
        fallback when no sidecar is used."""
        rev = cls(video, settings)
        js, npz = sidecar_paths(video)
        if not (js.exists() or npz.exists()):
            return rev

        def ignore(why: str) -> "PupilReview":
            rev.sidecar_note = f"existing results not loaded: {why}"
            rev._keep_old_sidecar = True
            return rev

        try:
            meta = json.loads(js.read_text(encoding="utf-8"))
            with np.load(npz) as data:
                auto, manual, blink = (np.asarray(data[k], dtype=dt) for k, dt in
                                       (("auto", float), ("manual", float),
                                        ("blink", bool)))
                gen = str(data["generation"]) if "generation" in data else None
        except Exception as e:                  # noqa: BLE001 — any bad file
            return ignore(f"unreadable ({type(e).__name__})")
        if not isinstance(meta, dict):
            return ignore("unreadable")
        n = len(rev)
        mine = rev._identity()
        if meta.get("frames") != n:
            return ignore(f"made for {meta.get('frames')} frames, clip has {n}")
        if not _same_name(str(meta.get("video", mine["video"])), mine["video"]):
            return ignore(f"made for {meta.get('video')}")
        if meta.get("video_bytes", mine["video_bytes"]) != mine["video_bytes"]:
            return ignore("the clip has changed since")
        if (auto.shape != (n, 5) or manual.shape != (n, 5)
                or blink.shape != (n,)):
            return ignore("unreadable")
        try:
            st = _settings_from(meta.get("settings"))
            tracked_with = (_settings_from(meta["tracked_with"])
                            if "tracked_with" in meta else None)
        except Exception:                       # noqa: BLE001 — bad values
            return ignore("unreadable settings")
        if st is not None:
            rev.settings = st
        rev.auto[:], rev.manual[:], rev.blink[:] = auto, manual, blink
        rev.tracked = bool(meta.get("tracked", True))
        if "tracked_with" in meta:
            rev.tracked_with = tracked_with
        else:   # an old sidecar has no record: assume the shown settings
            rev.tracked_with = rev.settings if rev.tracked else None
        if gen is not None and meta.get("generation") != gen:
            # Half-written pair: the fits can't be tied to any settings, so
            # they show as stale until re-tracked.
            rev.tracked_with = None
            rev.sidecar_note = ("the saved fits and settings don't match "
                                "(an interrupted save?) — re-track")
        return rev
