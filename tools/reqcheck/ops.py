"""Operations that write requirement files: new, finalize, baseline."""
from __future__ import annotations

import copy
import datetime
import os
from dataclasses import dataclass, field

from . import registry as reg
from .gitio import from_worktree
from .rules import (NOT_IN_SET_STATUSES, PATH_RE, State, _docs, check_transition,
                    check_tree)
from .schema import (BASELINE_CHANGE_PREFIX, current_snapshot, new_doc, parse, render,
                     split_id, validate_doc)


def _read(root, rel):
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def today() -> str:
    return datetime.date.today().isoformat()


def req_path(req_id: str) -> str:
    return f"requirements/level-{split_id(req_id)[0]}/{req_id}.md"


def next_id(root, level):
    return reg.next_id(reg.load_ids(_read(root, reg.IDS_FILE)), level)


def create(root, level, title, owner, *, date=None, **fields):
    """Allocate the next id, register it and write revision 1. Returns the path."""
    ids_text = _read(root, reg.IDS_FILE)
    ids = reg.load_ids(ids_text)
    date = date or today()
    req_id = reg.next_id(ids, level)
    doc = new_doc(req_id, title, owner, date, **fields)
    errs = validate_doc(doc)
    if errs:
        raise ValueError("; ".join(errs))
    ids.append({"id": req_id, "issued": date})
    _write(root, reg.IDS_FILE, reg.dump_ids(ids))
    if _read(root, reg.SETS_FILE) is None:
        _write(root, reg.SETS_FILE, reg.dump_sets([]))
    _write(root, req_path(req_id), render(doc))
    return req_path(req_id)


def _append_revision(doc, sets, change, reason, author, date, *, invalidated_date=None):
    """Bump `doc` to the next revision: clear its requirement_set, append one History entry
    with the full snapshot of the current content and mark the closed set it belonged to
    as invalidated (in `sets`, modified in place). Returns the old set tag or None."""
    old_set = doc.fm["requirement_set"]
    if old_set:
        for s in sets:
            if s["tag"] == old_set and s["status"] == "closed":
                s["status"] = "invalidated"
                s["invalidated"] = invalidated_date or date
                s["invalidated_by"] = f"{doc.id} revised to revision {doc.fm['revision'] + 1}"
        doc.fm["requirement_set"] = None
    doc.fm["revision"] += 1
    doc.history.append({
        "revision": doc.fm["revision"], "date": date, "author": author,
        "set": None, "change": change, "reason": reason, "snapshot": current_snapshot(doc),
    })
    errs = validate_doc(doc)
    if errs:
        raise ValueError("; ".join(errs))
    return old_set


def baseline(root, tag, release_url, commit, *, author="github-actions[bot]", date=None,
             dry_run=False):
    """Close a new requirement set from all set-specific requirements."""
    errs = check_tree(from_worktree(root))
    if errs:
        raise ValueError("tree check failed:\n  " + "\n  ".join(errs))
    sets = reg.load_sets(_read(root, reg.SETS_FILE))
    if any(s["tag"] == tag for s in sets):
        raise ValueError(f"set {tag} already exists")
    date = date or today()
    members, writes = [], []
    state = from_worktree(root)
    for path in sorted(state.files):
        doc = parse(state.files[path])
        if not doc.fm["set_specific"] or doc.fm["status"] in NOT_IN_SET_STATUSES:
            continue
        doc.fm["requirement_set"] = tag
        doc.fm["revision"] += 1
        doc.history.append({
            "revision": doc.fm["revision"], "date": date, "author": author, "set": tag,
            "change": f"{BASELINE_CHANGE_PREFIX} {tag} (release {release_url})",
            "reason": f"GitHub release {tag} was published.",
            "snapshot": current_snapshot(doc),
        })
        members.append((doc.id, doc.fm["revision"]))
        writes.append((path, render(doc)))
    if not members:
        raise ValueError("no set-specific requirements to baseline")
    for s in sets:
        if s["status"] == "closed":
            s["status"] = "superseded"
            s["superseded_by"] = tag
    sets.append(reg.new_set(tag, release_url, commit, date, members))
    if not dry_run:
        for path, text in writes:
            _write(root, path, text)
        _write(root, reg.SETS_FILE, reg.dump_sets(sets))
    return members


# --------------------------------------------------------------------------- finalize

MUTABLE_KEYS = ["title", "status", "priority", "category", "verification", "parents",
                "owner", "set_specific", "tags"]
NEW_CHANGE = "New requirement."


@dataclass
class FinalizeResult:
    files: dict                  # requirement path -> text, the complete result
    ids_text: str | None
    sets_text: str | None
    delete: list = field(default_factory=list)   # tip paths that no longer exist (renamed)
    revised: list = field(default_factory=list)  # [(id, new revision)]
    created: list = field(default_factory=list)  # [(branch id, final id)]

    def differences(self, tip: State) -> list:
        """What applying this result would change in the tip, as readable lines."""
        out = [f"{p}: {'new' if p not in tip.files else 'rewritten'}"
               for p in sorted(self.files) if tip.files.get(p) != self.files[p]]
        out += [f"{p}: removed (renumbered)" for p in sorted(self.delete)]
        if self.ids_text != tip.ids_text:
            out.append(f"{reg.IDS_FILE}: rewritten")
        if self.sets_text != tip.sets_text:
            out.append(f"{reg.SETS_FILE}: rewritten")
        return out


def _legacy_snapshot(snap):
    """Older snapshots carry `parent` instead of `parents`; normalise for comparison."""
    if "parent" not in snap:
        return snap
    out = {}
    for k, v in snap.items():
        if k == "parent":
            out["parents"] = list(v) if isinstance(v, list) else ([v] if v else [])
        else:
            out[k] = v
    return out


def _content(snap):
    s = _legacy_snapshot(snap)
    return {k: v for k, v in s.items() if k != "requirement_set"}


def _pick(block, rid, key, aliases=()):
    for r in (rid, *aliases):
        v = (block.per_id.get(r) or {}).get(key)
        if v:
            return v
    return getattr(block, key)


def finalize(main: State, tip: State, block, *, date, pr_url=None) -> FinalizeResult:
    """Turn the branch `tip` into the state that may be merged into `main`: one new
    revision per requirement whose content differs from main, a clean revision 1 for new
    requirements (renumbered when main took their id). Pure: nothing is written.
    `block` is the parsed PR block (prblock.PRBlock) or None. Raises ValueError."""
    if main.ids_text is None or tip.ids_text is None:
        raise ValueError(f"both main and the branch need {reg.IDS_FILE}")
    m_ids = reg.load_ids(main.ids_text)
    sets = copy.deepcopy(reg.load_sets(main.sets_text))
    t_sets = {s["tag"]: s for s in reg.load_sets(tip.sets_text)}
    m_docs, m_paths, errs = _docs(main)
    t_docs, t_paths, terrs = _docs(tip, branch=True)
    if errs or terrs:
        raise ValueError("cannot finalize, files are invalid:\n  " + "\n  ".join(errs + terrs))
    # classify the branch's requirements
    existing, new = {}, []
    for i, td in t_docs.items():
        md = m_docs.get(i)
        if md is not None and md.history[0] == td.history[0]:
            existing[i] = (md, td)
        else:
            new.append(td)
    gone = sorted(i for i in m_docs if i not in t_docs)
    if gone:
        raise ValueError("requirements of main are missing on the branch (files may never be "
                         "deleted): " + ", ".join(gone))
    new.sort(key=lambda d: (d.fm["level"], split_id(d.id)[1]))
    # renumbering (parents of branch files follow)
    registry = list(m_ids)
    mapping, final_ids = {}, {}
    for td in new:
        nid = reg.next_id(registry, td.fm["level"])
        registry.append({"id": nid, "issued": date})
        final_ids[td.id] = nid
        if nid != td.id:
            mapping[td.id] = nid

    def fix(parents):
        return [mapping.get(p, p) for p in parents]

    # existing requirements whose content differs from main's newest snapshot
    changed = {}
    for i, (md, td) in existing.items():
        cand = _with_content(md, td, fix)
        if _content(current_snapshot(cand)) != _content(md.history[-1]["snapshot"]):
            changed[i] = (md, cand, td)
    problems = []
    if (changed or new) and (block is None or not block.author):
        problems.append("the PR description needs a '## Requirement changes' section with "
                        "an 'Author:' line")
    sfx = f" (PR {pr_url})" if pr_url else ""
    entries = {}
    miss_c, miss_r = [], []
    for i in sorted(changed):
        ch = _pick(block, i, "change") if block else None
        rs = _pick(block, i, "reason") if block else None
        if not ch:
            miss_c.append(i)
        if not rs:
            miss_r.append(i)
        entries[i] = (ch, rs and rs + sfx)
    if miss_c or miss_r:
        problems.append("no Change/Reason in the PR description for changed requirement(s): "
                        + ", ".join(sorted(set(miss_c + miss_r)))
                        + " (add default 'Change:'/'Reason:' lines or a line "
                        "'- <id>: Change: ... | Reason: ...' for each)")
    if problems:
        raise ValueError("; ".join(problems))

    files, revised, created = {}, [], []
    for i, md in m_docs.items():
        files[m_paths[i]] = main.files[m_paths[i]]   # unchanged unless revised below
    for i in sorted(changed):
        md, cand, td = changed[i]
        ch, rs = entries[i]
        doc = _revise_idempotent(md, cand, td, sets, ch, rs, block.author, date,
                                 tip.files.get(t_paths[i]))
        files[m_paths[i]] = render(doc)
        revised.append((i, doc.fm["revision"]))
    new_entries, delete = [], []
    for td in new:
        nid = final_ids[td.id]
        per_rs = _pick(block, td.id, "reason", (nid,)) if block else None
        ch = ((block.per_id.get(td.id) or block.per_id.get(nid) or {}).get("change")
              if block else None) or NEW_CHANGE
        rs = (per_rs + sfx) if per_rs else td.history[0]["reason"]
        doc = _new_from(td, nid, fix, ch, rs, block.author, date, tip.files.get(t_paths[td.id]))
        files[req_path(nid)] = render(doc)
        new_entries.append({"id": nid, "issued": doc.fm["created"]})
        if nid != td.id:
            delete.append(t_paths[td.id])
        created.append((td.id, nid))
    # a branch file whose path is reused by main's requirement is replaced, not deleted
    delete = [p for p in delete if p not in files]
    # sets: keep the branch's invalidation date when everything else matches
    sets_text = main.sets_text
    if any(a != b for a, b in zip(sets, reg.load_sets(main.sets_text))):
        for s in sets:
            t = t_sets.get(s["tag"])
            if (s["status"] == "invalidated" and t and t["status"] == "invalidated"
                    and t["invalidated_by"] == s["invalidated_by"]):
                s["invalidated"] = t["invalidated"]
        sets_text = reg.dump_sets(sets)
    ids_text = reg.dump_ids(m_ids + new_entries) if new_entries else main.ids_text
    res = FinalizeResult(files, ids_text, sets_text, delete, revised, created)
    out = State(files=dict(files), ids_text=ids_text, sets_text=sets_text,
                document_text=tip.document_text)
    bad = check_tree(out) + check_transition(main, out)
    if bad:
        raise ValueError("the finalized result violates the rules:\n  " + "\n  ".join(bad))
    return res


def _with_content(md, td, fix):
    """Main's document with the branch's content applied (parents renumbered)."""
    cand = copy.deepcopy(md)
    for k in MUTABLE_KEYS:
        cand.fm[k] = copy.deepcopy(td.fm[k])
    cand.fm["parents"] = fix(cand.fm["parents"])
    cand.text = dict(td.text)
    return cand


def _revise_idempotent(md, cand, td, sets, change, reason, author, date, tip_text):
    """Append the revision. If the branch already carries the identical revision except
    for its date, keep that date so that a second run changes nothing."""
    n = len(md.history)
    if len(td.history) == n + 1 and td.history[:n] == md.history:
        old_date = td.history[-1]["date"]
        trial, trial_sets = copy.deepcopy(cand), copy.deepcopy(sets)
        _append_revision(trial, trial_sets, change, reason, author, old_date)
        if render(trial) == tip_text:
            date = old_date
    _append_revision(cand, sets, change, reason, author, date)
    return cand


def _new_from(td, nid, fix, change, reason, author, date, tip_text):
    def mk(created):
        f = td.fm
        return new_doc(nid, f["title"], f["owner"], created, status=f["status"],
                       priority=f["priority"], category=f["category"],
                       verification=f["verification"], parents=fix(f["parents"]),
                       set_specific=f["set_specific"], tags=f["tags"],
                       statement=td.text["statement"], rationale=td.text["rationale"],
                       notes=td.text["notes"], change=change, reason=reason, author=author)
    h = td.history[0]
    if len(td.history) == 1 and (h["author"], h["change"], h["reason"]) == (author, change, reason) \
            and h["date"] == td.fm["created"]:
        return mk(td.fm["created"])  # already finalized earlier: keep its dates
    return mk(date)


def apply_finalize(root, tip: State, res: FinalizeResult):
    """Write a FinalizeResult into the worktree. Returns the changed paths."""
    done = []
    for p in sorted(res.delete):
        os.remove(os.path.join(root, p))
        done.append(p)
    for p, text in sorted(res.files.items()):
        if tip.files.get(p) != text:
            _write(root, p, text)
            done.append(p)
    for rel, text, old in ((reg.IDS_FILE, res.ids_text, tip.ids_text),
                           (reg.SETS_FILE, res.sets_text, tip.sets_text)):
        if text is not None and text != old:
            _write(root, rel, text)
            done.append(rel)
    return done
