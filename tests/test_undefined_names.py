"""Every name the app uses resolves to something.

Guards the moved-code defect: a file is split, the new module's imports miss a
name the moved code used, and the NameError waits on a path the suite never
calls. Happened 2026-08-13 (six names, `stage/settings.py` and `dmd/control.py`).

`symtable` does the scoping; this asks whether what's left lands on a
module-level binding or a builtin. Nothing is imported or executed.

Boundary: under `from __future__ import annotations` the annotations are strings
and out of scope, so an annotation-only import is not defended. Asserted below.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_undefined_names.py
"""
from __future__ import annotations

import ast
import builtins
import symtable
import sys
from pathlib import Path

from _harness import APP_DIR, Report

# `__conditional_annotations__` is a 3.14 compiler artifact, not a real name.
RESOLVES = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__debug__", "__path__", "__annotations__", "__module__",
    "__qualname__", "__class__", "__dict__", "__conditional_annotations__",
}
SKIP_DIRS = {".venv", "__pycache__", ".git"}


def _module_bindings(top: symtable.SymbolTable) -> set[str]:
    names = {s.get_name() for s in top.get_symbols()
             if s.is_assigned() or s.is_imported() or s.is_namespace()}
    _add_declared_globals(top, names)
    return names


def _add_declared_globals(table: symtable.SymbolTable, names: set[str]) -> None:
    """`global CACHE; CACHE = {}` binds at module level too."""
    for s in table.get_symbols():
        if s.is_declared_global() and s.is_assigned():
            names.add(s.get_name())
    for child in table.get_children():
        _add_declared_globals(child, names)


def _walk(table: symtable.SymbolTable, bound: set[str],
          out: list[tuple[str, str]], scope: str) -> None:
    for s in table.get_symbols():
        # Local, parameter, closure and bound-here are resolved by construction;
        # what survives must come from module or builtin scope.
        if not s.is_referenced():
            continue
        if (s.is_local() or s.is_parameter() or s.is_free()
                or s.is_imported() or s.is_assigned()):
            continue
        if s.get_name() not in bound and s.get_name() not in RESOLVES:
            out.append((scope or "<module>", s.get_name()))
    for child in table.get_children():
        sub = f"{scope}.{child.get_name()}" if scope else child.get_name()
        _walk(child, bound, out, sub)


def scan_source(src: str, label: str = "<source>") -> tuple[str, list[tuple]]:
    """-> ("ok"|"star", [(scope, name, [lineno, ...])]).

    `import *` makes the bindings unknowable, so the file is reported skipped
    rather than silently passed.
    """
    tree = ast.parse(src, label)
    if any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
           for n in ast.walk(tree)):
        return "star", []
    top = symtable.symtable(src, label, "exec")
    found: list[tuple[str, str]] = []
    _walk(top, _module_bindings(top), found, "")
    return "ok", [(scope, name, _lines_of(tree, name)) for scope, name in found]


def _lines_of(tree: ast.AST, name: str) -> list[int]:
    return sorted({n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and n.id == name
                   and isinstance(n.ctx, ast.Load)})


def _runtime_loads(tree: ast.AST) -> set[str]:
    """Names read outside an annotation — the ones 3.14 evaluates."""
    anns: list[ast.AST] = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.AnnAssign, ast.arg)) and n.annotation is not None:
            anns.append(n.annotation)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns:
            anns.append(n.returns)
    deferred = {id(m) for a in anns for m in ast.walk(a)
                if isinstance(m, ast.Name)}
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and id(n) not in deferred}


def app_sources() -> list[Path]:
    return [p for p in sorted(APP_DIR.rglob("*.py"))
            if not SKIP_DIRS & set(p.parts)]


# ── the real package ──────────────────────────────────────────────────────────

def check_package(r: Report) -> None:
    files = app_sources()
    # A glob matching nothing would make every check here vacuous.
    r.check(len(files) > 60, f"the scan reaches the whole package ({len(files)} files)")
    for must in ("main.py", "devices.py", "adapters/base.py", "dmd/control.py",
                 "stage/settings.py", "pupil_cam/tracking.py",
                 "closed_loop/settings.py"):
        r.check(APP_DIR / must in files, f"{must} is in the scan")

    bad, starred, unparsed = [], [], []
    for p in files:
        rel = p.relative_to(APP_DIR).as_posix()
        try:
            status, hits = scan_source(p.read_text(encoding="utf-8"), rel)
        except SyntaxError as e:                # also the compile check
            unparsed.append(f"{rel}:{e.lineno}: {e.msg}")
            continue
        if status == "star":
            starred.append(rel)
            continue
        bad += [f"{rel}:{lines or '?'} {name!r} in {scope}"
                for scope, name, lines in hits]

    for label, lines in (("every file parses", unparsed),
                         ("every name resolves", bad),
                         ("no `import *` opts a file out", starred)):
        r.check(not lines, f"{label} ({len(lines)} found)")
        for line in lines:
            r.info(line)


# ── controls ──────────────────────────────────────────────────────────────────

MUST_FLAG = {
    "a split left the import behind":
        "from math import sqrt\ndef f(a):\n    return sqrt(a) + hypot(a, a)\n",
    "undefined inside a method":
        "class C:\n    def m(self):\n        return QtWidgets.QLabel()\n",
    "undefined in a comprehension":
        "def f(xs):\n    return [transform(x) for x in xs]\n",
    "undefined in a default argument":
        "def f(a=DEFAULT_PAD):\n    return a\n",
    "undefined in an except handler":
        "def f():\n    try:\n        pass\n    except OSError:\n"
        "        raise StageControllerError('x')\n",
    "undefined in a nested function":
        "def outer():\n    def inner():\n        return helper()\n    return inner\n",
    "undefined in a class body":
        "class C:\n    PAD = _PAD\n",
    "undefined decorator":
        "@memoize\ndef g():\n    pass\n",
}

# Valid scoping. A checker that cries wolf gets deleted.
MUST_NOT_FLAG = {
    "closure over an enclosing local":
        "def outer():\n    n = 1\n    def inner():\n        return n\n    return inner\n",
    "global declared and assigned in a function":
        "def init():\n    global CACHE\n    CACHE = {}\ndef get():\n    return CACHE\n",
    "try/except ImportError fallback":
        "try:\n    import numpy as np\nexcept ImportError:\n    np = None\n"
        "def f():\n    return np\n",
    "TYPE_CHECKING import used in an annotation":
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from acqApp.devices import CameraWorker\n"
        "def f(w: 'CameraWorker') -> None:\n    return None\n",
    "def in both branches of a conditional":
        "import sys\nif sys.platform == 'win32':\n    def pick():\n        return 1\n"
        "else:\n    def pick():\n        return 2\nx = pick()\n",
    "walrus in a comprehension":
        "def f(xs):\n    return [y for x in xs if (y := x * 2) > 3]\n",
    "exception alias":
        "def f():\n    try:\n        pass\n    except OSError as e:\n"
        "        return str(e)\n",
    "class attribute through self":
        "class C:\n    PAD = 4\n    def m(self):\n        return self.PAD\n",
    "builtins and module dunders":
        "def f():\n    print(__file__, __name__, len([]), isinstance(1, int))\n",
    "name bound by a for loop":
        "def f(xs):\n    for item in xs:\n        pass\n    return item\n",
    "name bound by with-as":
        "def f(p):\n    with open(p) as fh:\n        return fh.read()\n",
    "lambda parameter":
        "f = lambda a, b=2: a + b\n",
    "class referenced before its def":
        "def make():\n    return Later()\nclass Later:\n    pass\n",
}


def check_controls(r: Report) -> None:
    for label, src in MUST_FLAG.items():
        _, hits = scan_source(src, label)
        r.check(bool(hits), f"caught: {label}")
    for label, src in MUST_NOT_FLAG.items():
        _, hits = scan_source(src, label)
        r.check(not hits, f"quiet on: {label}"
                + (f"  <- false positive {[h[1] for h in hits]}" if hits else ""))

    status, hits = scan_source(
        "from os.path import *\ndef f():\n    return join('a', 'b')\n", "star")
    r.check(status == "star" and not hits, "an `import *` file reports as skipped")

    # Boundary, asserted not just described: under `from __future__ import
    # annotations` (most files here) annotations are strings and invisible to
    # this scan — `adapters/base.py` imports `Any` on exactly those terms.
    # Without it they are live expressions and are checked.
    fut = "from __future__ import annotations\n"
    _, quiet = scan_source(fut + "def f(x: Missing) -> None: pass\n", "ann")
    _, live = scan_source("def f(x: Missing) -> None: pass\n", "ann-live")
    r.check(not quiet, "a stringified annotation is out of scope")
    r.check(bool(live), "a live annotation is checked")


def check_injection(r: Report) -> None:
    """Break real files the way a split breaks them: drop one used import."""
    for rel in ("main.py", "dialogs.py", "adapters/base.py", "dmd/control.py",
                "stage/panel.py", "pupil_cam/tracking.py",
                "closed_loop/worker.py"):
        if not (APP_DIR / rel).is_file():
            # Report, don't crash: a moved file must fail this test loudly, not
            # take the run down with a traceback.
            r.check(False, f"[{rel}] target has moved — update this list")
            continue
        src = (APP_DIR / rel).read_text(encoding="utf-8")
        broken, dropped = _drop_a_runtime_import(src)
        if broken is None:
            r.check(False, f"[{rel}] no runtime import to drop — no control")
            continue
        _, hits = scan_source(broken, rel)
        names = {name for _, name, _ in hits}
        r.check(bool(names & set(dropped)),
                f"[{rel}] dropping `{', '.join(dropped)}` is caught "
                f"(reported {sorted(names) or 'nothing'})")
        # Paired, so the difference is the injected defect and not a noisy file.
        _, clean = scan_source(src, rel)
        r.check(not clean, f"[{rel}] the real file itself is clean")


def _drop_a_runtime_import(src: str) -> tuple[str | None, list[str]]:
    """Blank the first module-level import read at runtime.

    Runtime, not merely present: `adapters/base.py`'s `Any` is annotation-only,
    so dropping it is not a defect and the scan is right to stay quiet.
    """
    tree = ast.parse(src)
    used = _runtime_loads(tree)
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if any(a.name == "*" for a in node.names):
            continue
        bound = [(a.asname or a.name).split(".")[0] for a in node.names]
        hit = [b for b in bound if b in used]
        if not hit:
            continue                    # `from __future__ import annotations`
        lines = src.splitlines(keepends=True)
        for i in range(node.lineno - 1, (node.end_lineno or node.lineno)):
            lines[i] = "\n"             # keep line numbering honest
        return "".join(lines), hit
    return None, []


def main() -> int:
    r = Report("undefined")
    check_controls(r)
    check_injection(r)
    check_package(r)
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
