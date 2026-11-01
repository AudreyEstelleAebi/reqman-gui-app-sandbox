"""CLI: python3 -m tools.reqcheck <command>. Run with -h for details."""
from __future__ import annotations

import argparse
import os
import sys

try:
    import yaml  # noqa: F401
except ImportError:
    sys.exit("PyYAML is missing: run `pip install -r requirements-dev.txt`")

from . import gitio, ops
from .prblock import parse_pr_block
from .rules import check_branch, check_transition, check_tree
from .schema import PRIORITIES, STATUSES, VERIFICATIONS, render_template


def report(errs, what):
    if errs:
        print(f"reqcheck: {len(errs)} problem(s) in {what}:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        print("Read RULES.md. Never bypass the hook with --no-verify.", file=sys.stderr)
        return 1
    print(f"reqcheck: {what} OK")
    return 0


def _ids(text):
    return [t.strip() for t in text.split(",") if t.strip()]


def cmd_staged(root):
    """Pre-commit. On a feature branch: branch rules for the index against the merge base
    with main. On main (or without any main ref): the strict rules against HEAD."""
    new = gitio.from_index(root)
    ref = gitio.main_ref(root)
    branch = gitio.current_branch(root)
    head = gitio.head_or_none(root)
    on_main = branch == "main" or (branch is None and ref and head
                                   and gitio.rev_parse("HEAD", root) == gitio.rev_parse(ref, root))
    base = gitio.merge_base(ref, "HEAD", root) if ref and head and not on_main else None
    if base is None:
        old = gitio.from_commit(head, root)
        return report(check_tree(new) + check_transition(old, new), "staged changes")
    errs = check_tree(new, branch=True) + check_branch(gitio.from_commit(base, root), new)
    return report(errs, f"staged changes (branch rules against {base[:8]})")


def cmd_branch(root, main_ref, head):
    errs, n = [], 0
    for sha, _ in gitio.commits(main_ref, head, root):
        n += 1
        base = gitio.merge_base(main_ref, sha, root)
        if base is None:
            errs.append(f"{sha[:8]}: no common ancestor with {main_ref}")
            continue
        new = gitio.from_commit(sha, root)
        es = check_tree(new, branch=True) + check_branch(gitio.from_commit(base, root), new)
        errs += [f"{sha[:8]}: {e}" for e in es]
    return report(errs, f"{n} commit(s) {main_ref}..{head}")


def cmd_finalize(root, a):
    if not gitio.rev_parse(a.main, root):
        print(f"reqcheck: unknown ref {a.main}", file=sys.stderr)
        return 1
    if not gitio.is_ancestor(a.main, "HEAD", root):
        print(f"reqcheck: {a.main} is not an ancestor of HEAD; merge {a.main} into the "
              "branch first (the PR must be up to date)", file=sys.stderr)
        return 1
    try:
        with open(a.pr_body, encoding="utf-8") as fh:
            block = parse_pr_block(fh.read())
        main = gitio.from_commit(a.main, root)
        tip = gitio.from_worktree(root)
        date = getattr(a, "date", None) or ops.today()
        res = ops.finalize(main, tip, block, date=date, pr_url=a.pr_url)
    except (ValueError, OSError) as exc:
        print(f"reqcheck: {exc}", file=sys.stderr)
        return 1
    if a.cmd == "finalize":
        changed = ops.apply_finalize(root, tip, res)
        for i, rev in res.revised:
            print(f"revised {i} to revision {rev}")
        for old, new in res.created:
            print(f"new requirement {old}" + (f" renumbered to {new}" if old != new else ""))
        print(f"finalize: {len(changed)} file(s) written" if changed
              else "finalize: nothing to change")
        return 0
    diff = res.differences(tip)
    errs = [f"not finalized, finalize would change: {d}" for d in diff]
    errs += [f"tip vs {a.main}: {e}" for e in check_tree(tip) + check_transition(main, tip)]
    return report(errs, f"gate (C2) against {a.main}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reqcheck")
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tree", help="check the working tree")
    t.add_argument("--branch", action="store_true",
                   help="branch mode: content may be mid-edit (newest snapshot need not match)")
    sub.add_parser("staged", help="pre-commit: branch rules for the index against "
                   "merge-base(origin/main, HEAD); strict rules against HEAD on main")
    r = sub.add_parser("range", help="check every commit in BASE..HEAD (main-line rules)")
    r.add_argument("base", help="base commit, or ROOT for the whole history")
    r.add_argument("head", nargs="?", default="HEAD")
    r.add_argument("--first-parent", action="store_true",
                   help="walk the first-parent chain only; merge commits are checked "
                   "against their first parent with the full transition rules")
    br = sub.add_parser("branch", help="check every commit in REF..HEAD with the branch rules "
                        "against merge-base(REF, commit)")
    br.add_argument("--main", required=True, metavar="REF", help="the main branch ref, e.g. origin/main")
    br.add_argument("head", nargs="?", default="HEAD")
    for name, hlp in (("finalize", "rewrite the working tree into the mergeable state "
                       "(idempotent)"),
                      ("gate", "exit 0 iff finalize would change nothing and the tip passes the "
                       "strict rules against main")):
        f = sub.add_parser(name, help=hlp)
        f.add_argument("--main", required=True, metavar="REF", help="the main branch ref, e.g. origin/main")
        f.add_argument("--pr-body", required=True, metavar="FILE",
                       help="file with the PR description (see '## Requirement changes' in RULES.md)")
        f.add_argument("--pr-url", default=None, help="appended to each Reason as ' (PR <url>)'")
        if name == "finalize":
            f.add_argument("--date", default=None, help="ISO date for new revisions (default: today)")
    sub.add_parser("template", help="print the TEMPLATE.md content")
    n = sub.add_parser("new", help="create a requirement with the next free id")
    n.add_argument("--level", type=int, required=True)
    n.add_argument("--title", required=True)
    n.add_argument("--owner", default=None, help="optional; may be empty")
    n.add_argument("--statement", default="TBD")
    n.add_argument("--rationale", default="TBD")
    n.add_argument("--notes", default="None")
    n.add_argument("--category", default="TBD")
    n.add_argument("--priority", default="Medium", choices=PRIORITIES)
    n.add_argument("--status", default="Draft", choices=STATUSES)
    n.add_argument("--verification", choices=VERIFICATIONS)
    n.add_argument("--parents", default="", help="comma separated requirement ids")
    n.add_argument("--tags", default="", help="comma separated slugs")
    n.add_argument("--no-set-specific", action="store_true")
    n.add_argument("--reason", default="New requirement.")
    n.add_argument("--author")
    nx = sub.add_parser("next-id", help="print the next free id (does not allocate)")
    nx.add_argument("--level", type=int, required=True)
    b = sub.add_parser("baseline", help="close a requirement set (release workflow)")
    b.add_argument("tag")
    b.add_argument("--release-url", required=True)
    b.add_argument("--commit", required=True)
    b.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = a.root

    if a.cmd == "tree":
        return report(check_tree(gitio.from_worktree(root), branch=a.branch), "working tree")
    if a.cmd == "staged":
        return cmd_staged(root)
    if a.cmd == "range":
        errs = []
        for sha, parents in gitio.commits(a.base, a.head, root, first_parent=a.first_parent):
            new = gitio.from_commit(sha, root)
            es = check_tree(new)
            if len(parents) <= 1 or a.first_parent:
                es += check_transition(gitio.from_commit(parents[0] if parents else None, root), new)
            errs += [f"{sha[:8]}: {e}" for e in es]
        return report(errs, f"commits {a.base}..{a.head}")
    if a.cmd == "branch":
        return cmd_branch(root, a.main, a.head)
    if a.cmd in ("finalize", "gate"):
        return cmd_finalize(root, a)
    if a.cmd == "template":
        sys.stdout.write(render_template())
        return 0
    try:
        if a.cmd == "next-id":
            print(ops.next_id(root, a.level))
        elif a.cmd == "new":
            tags = [t for t in a.tags.split(",") if t]
            print(ops.create(
                root, a.level, a.title, a.owner, statement=a.statement,
                rationale=a.rationale, notes=a.notes, category=a.category,
                priority=a.priority, status=a.status, verification=a.verification,
                parents=_ids(a.parents), tags=tags, set_specific=not a.no_set_specific,
                reason=a.reason, author=a.author))
        elif a.cmd == "baseline":
            members = ops.baseline(root, a.tag, a.release_url, a.commit, dry_run=a.dry_run)
            print(("would baseline" if a.dry_run else "baselined") + f" {len(members)} requirements into {a.tag}")
            for i, rev in members:
                print(f"  {i} revision {rev}")
    except ValueError as exc:
        print(f"reqcheck: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
