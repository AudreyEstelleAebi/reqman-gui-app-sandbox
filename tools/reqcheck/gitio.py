"""Build rule States from git commits, the index or the working tree."""
from __future__ import annotations

import os
import subprocess

from .document import DOC_FILE
from .registry import IDS_FILE, SETS_FILE
from .rules import State


def git(args, cwd=".", check=True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _wanted(path):
    return path.startswith("requirements/") or path in (IDS_FILE, SETS_FILE, DOC_FILE)


def _build(paths, read) -> State:
    st = State()
    for p in paths:
        if not _wanted(p):
            continue
        text = read(p)
        if p == IDS_FILE:
            st.ids_text = text
        elif p == SETS_FILE:
            st.sets_text = text
        elif p == DOC_FILE:
            st.document_text = text
        else:
            st.files[p] = text
    return st


def from_commit(rev, cwd=".") -> State:
    if rev is None:
        return State()
    names = git(["ls-tree", "-r", "--name-only", "-z", rev], cwd).split("\0")
    return _build([n for n in names if n], lambda p: git(["show", f"{rev}:{p}"], cwd))


def from_index(cwd=".") -> State:
    names = git(["ls-files", "-z"], cwd).split("\0")
    return _build([n for n in names if n], lambda p: git(["show", f":{p}"], cwd))


def from_worktree(root=".") -> State:
    paths = []
    for base, _, files in os.walk(os.path.join(root, "requirements")):
        for f in files:
            paths.append(os.path.relpath(os.path.join(base, f), root).replace(os.sep, "/"))
    for f in (IDS_FILE, SETS_FILE, DOC_FILE):
        if os.path.exists(os.path.join(root, f)):
            paths.append(f)

    def read(p):
        with open(os.path.join(root, p), encoding="utf-8", newline="") as fh:
            return fh.read()
    return _build(paths, read)


def head_or_none(cwd="."):
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=cwd,
                       capture_output=True, text=True)
    return "HEAD" if r.returncode == 0 else None


def commits(base, head, cwd=".", first_parent=False):
    """[(sha, [parents])] oldest first. base None/'ROOT' means from the first commit.

    With first_parent=True only the first-parent chain is walked (merge commits
    appear once; the commits merged in are not listed)."""
    args = ["rev-list", "--reverse", "--parents"]
    args.append("--first-parent" if first_parent else "--topo-order")
    args += [head] if base in (None, "ROOT") else [f"{base}..{head}"]
    out = []
    for line in git(args, cwd).splitlines():
        sha, *parents = line.split()
        out.append((sha, parents))
    return out


def rev_parse(rev, cwd="."):
    """Full sha of a commit-ish, or None when it does not exist."""
    r = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], cwd=cwd,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def merge_base(a, b, cwd="."):
    """Best common ancestor of two commits, or None when there is none."""
    r = subprocess.run(["git", "merge-base", a, b], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def is_ancestor(ancestor, descendant, cwd="."):
    r = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd,
                       capture_output=True, text=True)
    return r.returncode == 0


def current_branch(cwd="."):
    """Short name of the checked-out branch, or None (detached HEAD / no commits)."""
    r = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=cwd,
                       capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def main_ref(cwd="."):
    """The ref that stands for main: origin/main, else main, else None."""
    for ref in ("origin/main", "main"):
        if rev_parse(ref, cwd):
            return ref
    return None
