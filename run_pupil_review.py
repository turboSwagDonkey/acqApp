"""Run the pupil review tool on its own, straight from a checkout.

    git clone https://github.com/turboSwagDonkey/acqApp.git     (folder must be "acqApp")
    python acqApp/run_pupil_review.py [clip.avi] [--update]

First run builds a private `.venv-pupil` here (requirements-pupil.txt), clones
EyeLoop beside the checkout (GPL-3.0, fetched not bundled) and applies
`docs/eyeloop-3.14-patches.diff`. `--update` does a fast-forward `git pull`
first, so the tool tracks the main branch. `--check` builds the window
headless and exits (for tests). Stdlib only until the environment is ready.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv-pupil"
NEEDS = ("PyQt6", "pyqtgraph", "numpy", "cv2", "yaml", "h5py", "tifffile")
PATCH = ROOT / "docs" / "eyeloop-3.14-patches.diff"
EYELOOP_URL = "https://github.com/simonarvin/eyeloop.git"


def _say(msg: str) -> None:
    print(f"[pupil review] {msg}", flush=True)


def missing_modules() -> list[str]:
    return [m for m in NEEDS if importlib.util.find_spec(m) is None]


def _venv_python() -> Path:
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return VENV.joinpath(*sub)


def ensure_env() -> int | None:
    """None = this interpreter is ready. Otherwise the private venv is built
    and this returns the exit code of the tool re-run inside it."""
    if not missing_modules():
        return None
    py = _venv_python()
    # sys.prefix, not the interpreter path: on POSIX the venv's python is a
    # symlink to the system one, so the resolved paths always match.
    if Path(sys.prefix).resolve() == VENV.resolve():
        _say(f"{missing_modules()} still missing in {VENV}; "
             f"delete it and run again")
        return 1
    if not py.exists():
        _say(f"creating {VENV} ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    _say("installing requirements-pupil.txt ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "-q", "-r",
                           str(ROOT / "requirements-pupil.txt")])
    return subprocess.call([str(py), str(Path(__file__).resolve()),
                            *sys.argv[1:]])


def eyeloop_dir() -> Path:
    return Path(os.environ.get("ACQAPP_EYELOOP_DIR") or ROOT.parent / "eyeloop")


def ensure_eyeloop() -> None:
    """Clone EyeLoop and patch it, once. Failure is reported, not fatal: the
    window still opens and 'Track' says what is missing."""
    clone = eyeloop_dir()
    try:
        if not (clone / "eyeloop").is_dir():
            _say(f"cloning EyeLoop to {clone} ...")
            subprocess.check_call(["git", "clone", "-q", EYELOOP_URL, str(clone)])
        patched = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(PATCH)],
            cwd=clone, capture_output=True).returncode == 0
        if not patched:
            subprocess.check_call(["git", "apply", "--whitespace=nowarn",
                                   str(PATCH)], cwd=clone)
            _say("applied the EyeLoop compatibility patch")
    except (OSError, subprocess.CalledProcessError) as e:
        _say(f"EyeLoop setup failed ({e}). Clone {EYELOOP_URL} to {clone} and "
             f"run `git apply {PATCH}` inside it, then start again.")


def update() -> None:
    try:
        subprocess.check_call(["git", "pull", "--ff-only"], cwd=ROOT)
    except (OSError, subprocess.CalledProcessError) as e:
        _say(f"update skipped ({e})")


def main(argv: list[str]) -> int:
    if ROOT.name != "acqApp":
        print('[pupil review] this folder must be named "acqApp" (the code '
              'imports itself by that name). Re-clone as acqApp.')
        return 1
    # Before any path is printed: one the console can't encode would raise.
    sys.path.insert(0, str(ROOT.parent))
    from acqApp.console import enable_safe_console
    enable_safe_console()
    if "--update" in argv:
        update()
    code = ensure_env()
    if code is not None:
        return code
    ensure_eyeloop()
    from acqApp.devices.pupil_cam.review_app import main as run
    return run([a for a in argv if a != "--update"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
