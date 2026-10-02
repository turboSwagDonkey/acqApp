# hdf5 — retired 2026-10-01

`HDF5Writer`: every stream of a session in one `.h5` (frames, timestamps,
values; metadata as typed attributes), with a direct-chunk fast path for the
voltage camera. It was the default; a folder of per-device files was the
option. Retired at the operator's request: sessions are now always a folder —
voltage camera as DCIMG (or TIFF), pupil camera as AVI, scalars as CSV,
settings as JSON (`acq/writer.py` `SessionWriter`). Nothing here is imported;
`tests/test_hdf5_writer.py` is not in `run_all`.

Still reading `.h5`: `saving/bpod_match.py` opens Bpod's own MATLAB v7.3
files with h5py (not ours), and `rename_trial` / `delete_recording` still
handle an old `.h5` session, so recordings made before this keep working.

To restore: move `hdf5_writer.py`'s classes back into `acq/writer.py`, the
tests back into `tests/test_saving.py` ("chunks" part), give `SaveConfig`
back `split`/`subfolder` and `resolve`/`resolve_routine` (`.h5` paths), the
Save tab its "Split into per-device files" check box, and `main.py` the
`SplitWriter() if sc.split else HDF5Writer()` choice. `git log` on this
folder finds the commit that moved it.
