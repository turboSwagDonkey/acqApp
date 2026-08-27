"""docs/STRUCTURE.md still describes this tree.

A structure doc is prose, and prose about layout rots the first time a file
moves — the 2026-08-14 regroup broke every relative link in docs/ and nothing
noticed. So both halves of that file are checked here:

  1. the tree block lists exactly the files on disk, and
  2. the mermaid arrows are exactly the imports the AST finds.

(2) is the half worth having. A diagram nobody verifies records what the
layering was *meant* to be; this one fails when an adapter starts importing
another adapter, or when something under acq/ imports upward.

  acqApp\\.venv\\Scripts\\python.exe acqApp\\tests\\test_structure.py
"""
from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict

from _harness import APP_DIR, Report

DOC = APP_DIR / "docs" / "STRUCTURE.md"
# routine_templates/ is the operator's saved protocols, written by the app
# at runtime like sessions/ — its contents are not part of the tree.
SKIP_DIRS = {".venv", "__pycache__", ".git", "sessions", "routine_templates"}
KEEP_SUFFIX = {".py", ".json", ".txt", ".md"}
# Root modules are their own node; these packages are one node each.
PACKAGES = {"acq", "adapters", "closed_loop", "devices", "routines", "saving"}
# tests/ imports everything by design and is not drawn; archive/ is dead code
# kept for reference and is not part of the layering.
UNDRAWN = {"tests", "archive"}


# ── the tree half ─────────────────────────────────────────────────────────────

def _fence(text: str, lang: str) -> str:
    m = re.search(rf"```{lang}\n(.*?)```", text, re.S)
    if not m:
        raise AssertionError(f"no ```{lang} block in {DOC.name}")
    return m.group(1)


def documented_paths(text: str) -> set[str]:
    """Indentation-driven: two spaces per level, a trailing / means directory."""
    out: set[str] = set()
    stack: list[str] = []
    for raw in _fence(text, "text").splitlines():
        if not raw.strip():
            continue
        name = raw.strip().split()[0]
        depth = (len(raw) - len(raw.lstrip())) // 2
        stack = stack[:depth]
        if name.endswith("/"):
            stack.append(name.rstrip("/"))
            continue
        # A continuation line of prose, not a filename.
        if "." not in name:
            continue
        out.add("/".join([*stack, name]))
    return out


def real_paths() -> set[str]:
    out = set()
    for p in APP_DIR.rglob("*"):
        if SKIP_DIRS & set(p.parts) or not p.is_file():
            continue
        if p.suffix not in KEEP_SUFFIX and p.name != ".gitignore":
            continue
        out.add(p.relative_to(APP_DIR).as_posix())
    return out - _inits(out)


def _inits(paths: set[str]) -> set[str]:
    """Every package has one; listing ten of them buries the tree in noise.

    Optional on both sides, so the doc may still call out the two that carry
    logic (the adapter registry, the lazy re-exports).
    """
    return {p for p in paths if p.rsplit("/", 1)[-1] == "__init__.py"}


# ── the arrows half ───────────────────────────────────────────────────────────

def _area(parts: tuple[str, ...]) -> str:
    return parts[0] if parts[0] in PACKAGES | UNDRAWN else parts[-1].removesuffix(".py")


def measured_edges() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for p in sorted(APP_DIR.rglob("*.py")):
        if SKIP_DIRS & set(p.parts):
            continue
        src = _area(p.relative_to(APP_DIR).parts)
        if src in UNDRAWN or src == "__init__":
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"), str(p))):
            for m in _imported(n):
                if not m.startswith("acqApp."):
                    continue
                dst = _area(tuple(m.split(".")[1:]))
                if dst != src:
                    edges[src].add(dst)
    return edges


def _imported(n: ast.AST) -> list[str]:
    if isinstance(n, ast.ImportFrom) and n.module:
        # `from acqApp import adapters` names the targets, not the module.
        if n.module == "acqApp":
            return [f"acqApp.{a.name}" for a in n.names]
        return [n.module]
    if isinstance(n, ast.Import):
        return [a.name for a in n.names]
    return []


def drawn_edges(text: str) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    body = re.sub(r"\[.*?\]", "", _fence(text, "mermaid"))   # drop node labels
    for a, b in re.findall(r"^\s*(\w+)\s*-->\s*(\w+)\s*$", body, re.M):
        edges[a].add(b)
    return edges


# ── checks ────────────────────────────────────────────────────────────────────

def check_tree(r: Report, text: str) -> None:
    doc, real = documented_paths(text), real_paths()
    doc -= _inits(doc)
    r.check(len(doc) > 60, f"the tree block parses ({len(doc)} entries)")
    for label, missing in (("every file on disk is in STRUCTURE.md", real - doc),
                           ("every entry in STRUCTURE.md exists", doc - real)):
        r.check(not missing, f"{label} ({len(missing)} off)")
        for m in sorted(missing):
            r.info(m)


def check_arrows(r: Report, text: str) -> None:
    drawn, real = drawn_edges(text), measured_edges()
    r.check(sum(len(v) for v in drawn.values()) > 15,
            f"the mermaid block parses ({sum(len(v) for v in drawn.values())} arrows)")
    for src in sorted(set(drawn) | set(real)):
        extra, absent = drawn[src] - real[src], real[src] - drawn[src]
        r.check(not extra, f"[{src}] every drawn arrow is a real import "
                           f"({sorted(extra) or 'ok'})")
        r.check(not absent, f"[{src}] every real import is drawn "
                            f"({sorted(absent) or 'ok'})")
    r.check(not real.get("acq"), "acq/ imports nothing in the app — it is the sink")


def check_controls(r: Report, text: str) -> None:
    """A checker that cannot fail is not a checker."""
    broken = text.replace("    probe --> devices\n", "")
    r.check("devices" not in drawn_edges(broken)["probe"],
            "control: deleting an arrow from the diagram is visible")
    added = text.replace("    main --> adapters\n",
                         "    main --> adapters\n    saving --> main\n")
    r.check("main" in drawn_edges(added)["saving"],
            "control: an arrow with no matching import is visible")
    dropped = re.sub(r"^  clock\.py.*\n", "", text, count=1, flags=re.M)
    r.check("acq/clock.py" not in documented_paths(dropped),
            "control: dropping a file from the tree is visible")
    r.check("acq/clock.py" in documented_paths(text),
            "…and it is there in the real file (so the control is not vacuous)")


def main() -> int:
    r = Report("structure")
    text = DOC.read_text(encoding="utf-8")
    check_tree(r, text)
    check_arrows(r, text)
    check_controls(r, text)
    return r.finish()


if __name__ == "__main__":
    from acqApp.console import enable_safe_console
    enable_safe_console()
    sys.exit(main())
