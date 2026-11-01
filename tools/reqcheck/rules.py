"""Machine-checked rules. Tree rules (A*) look at one state of the repository,
transition rules (B*) compare two states (e.g. HEAD and the index)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import registry as reg
from .document import DOC_FILE, check_document
from .schema import BASELINE_CHANGE_PREFIX, current_snapshot, parse, split_id, validate_doc, yload

PATH_RE = re.compile(r"^requirements/level-(0|[1-9]\d*)/(LP-(?:0|[1-9]\d*)-[1-9]\d*)\.md$")
NOT_IN_SET_STATUSES = ("Withdrawn", "Rejected")


@dataclass
class State:
    files: dict = field(default_factory=dict)   # requirements/** path -> text
    ids_text: str | None = None
    sets_text: str | None = None
    document_text: str | None = None


def _docs(state, branch=False):
    """-> (docs by id, path by id, errors). branch=True: the content may be mid-edit, so
    the newest snapshot need not equal it yet (the finalize step records that)."""
    docs, paths, errs = {}, {}, []
    for path, text in state.files.items():
        m = PATH_RE.match(path)
        if not m:
            errs.append(f"{path}: only requirements/level-<level>/<id>.md files may live "
                        "under requirements/ (A2)")
            continue
        try:
            doc = parse(text)
        except ValueError as exc:
            errs.append(f"{path}: {exc} (A1)")
            continue
        errs += [f"{path}: {x} (A1/A6)" for x in validate_doc(doc, check_last_snapshot=not branch)]
        if doc.id != m.group(2):
            errs.append(f"{path}: id {doc.id!r} must equal the file name {m.group(2)!r} (A2)")
        if doc.fm["level"] != int(m.group(1)):
            errs.append(f"{path}: level {doc.fm['level']} does not match its folder (A2)")
        if doc.id in docs:
            errs.append(f"{path}: duplicate id {doc.id} (A3)")
        docs[doc.id] = doc
        paths[doc.id] = path
    return docs, paths, errs


def check_tree(state: State, branch: bool = False) -> list:
    """Tree rules. branch=True relaxes A6 'newest snapshot = content' (and the matching
    requirement_set/History check): on a branch the content is edited freely and the
    finalize step writes the revision later."""
    if state.ids_text is None:
        return []  # versioning not introduced yet in this state
    errs = []
    try:
        ids = reg.load_ids(state.ids_text)
        sets = reg.load_sets(state.sets_text)
    except ValueError as exc:
        return [str(exc)]
    docs, paths, derrs = _docs(state, branch)
    errs += derrs
    # A3/A4: registry
    seen = set()
    for e in ids:
        if e["id"] in seen:
            errs.append(f"{reg.IDS_FILE}: duplicate id {e['id']} (A3)")
        seen.add(e["id"])
    for i in sorted(seen - set(docs)):
        errs.append(f"{reg.IDS_FILE}: {i} has no requirement file (deleted?) (A4)")
    for i in sorted(set(docs) - seen):
        errs.append(f"{paths[i]}: id {i} is not in {reg.IDS_FILE}; allocate ids with "
                    "`reqcheck new` (A4)")
    levels = {}
    for i in seen:
        lv, n = split_id(i)
        levels.setdefault(lv, []).append(n)
    for lv, nums in levels.items():
        if sorted(nums) != list(range(1, max(nums) + 1)):
            errs.append(f"{reg.IDS_FILE}: numbers at level {lv} must be contiguous "
                        f"1..{max(nums)} (A4)")
    # A5 parents
    graph = {}
    for i, d in docs.items():
        ps = d.fm["parents"]
        if not isinstance(ps, list):
            continue  # reported by validate_doc
        graph[i] = [p for p in ps if isinstance(p, str)]
        for p in graph[i]:
            if p not in seen:
                errs.append(f"{paths[i]}: parent {p} does not exist (A5)")
    errs += _cycles(graph, paths)
    # D1 document metadata
    if state.document_text is not None:
        cats = {}
        for d in docs.values():
            cats.setdefault(d.fm["level"], set()).add(d.fm["category"])
        errs += check_document(state.document_text, cats)
    # A8 sets
    tags = {}
    closed = 0
    for s in sets:
        tag = s["tag"]
        if not isinstance(tag, str) or not tag or tag in tags:
            errs.append(f"{reg.SETS_FILE}: set tags must be non-empty and unique (A8)")
            continue
        tags[tag] = s
        if s["status"] not in reg.SET_STATUSES:
            errs.append(f"{reg.SETS_FILE}: set {tag} has bad status {s['status']!r} (A8)")
        closed += s["status"] == "closed"
        if s["status"] == "superseded" and not s["superseded_by"]:
            errs.append(f"{reg.SETS_FILE}: superseded set {tag} needs superseded_by (A8)")
        if s["status"] == "invalidated" and not (s["invalidated"] and s["invalidated_by"]):
            errs.append(f"{reg.SETS_FILE}: invalidated set {tag} needs invalidated and "
                        "invalidated_by (A8)")
        for k in ("release", "released_commit", "closed"):
            if not s[k]:
                errs.append(f"{reg.SETS_FILE}: set {tag} needs {k} (A8)")
        if not s["members"]:
            errs.append(f"{reg.SETS_FILE}: set {tag} has no members (A8)")
        for m in s["members"] or []:
            d = docs.get(m.get("id"))
            if d is None:
                errs.append(f"{reg.SETS_FILE}: set {tag} member {m.get('id')} does not exist (A8)")
                continue
            ent = [x for x in d.history if x["revision"] == m.get("revision")]
            if not ent or ent[0]["set"] != tag:
                errs.append(f"{reg.SETS_FILE}: set {tag} lists {m['id']} revision "
                            f"{m.get('revision')}, but that revision is not tagged {tag} (A8)")
    if closed > 1:
        errs.append(f"{reg.SETS_FILE}: at most one set may be closed (A8)")
    # A7 requirement_set references
    for i, d in docs.items():
        rs = d.fm["requirement_set"]
        if rs is None:
            continue
        s = tags.get(rs)
        if not d.fm["set_specific"]:
            errs.append(f"{paths[i]}: requirement_set must be empty unless set_specific (A7)")
        elif s is None:
            errs.append(f"{paths[i]}: requirement_set {rs!r} is not a registered set (A7)")
        elif {"id": i, "revision": d.fm["revision"]} not in s["members"]:
            errs.append(f"{paths[i]}: set {rs} does not list {i} at revision "
                        f"{d.fm['revision']} (A7)")
    return errs


def _cycles(graph, paths):
    """A5: the parent graph must be acyclic (self references are caught per file)."""
    errs, done, active = [], set(), []

    def visit(i):
        if i in done:
            return
        if i in active:
            cyc = active[active.index(i):] + [i]
            errs.append(f"{paths[i]}: parents form a cycle {' -> '.join(cyc)} (A5)")
            return
        active.append(i)
        for p in graph.get(i, []):
            if p != i and p in graph:
                visit(p)
        active.pop()
        done.add(i)

    for i in sorted(graph):
        visit(i)
    return errs


def _ids_in(state):
    out = {}
    for p, t in state.files.items():
        try:
            fm = yload(t.split("\n---\n", 1)[0].removeprefix("---\n"))
            out[fm["id"]] = p
        except Exception:
            pass
    return out


def _check_registry_append(old, old_ids, new_ids, rule):
    old_files = set(_ids_in(old))
    kept = [e for e in old_ids if e["id"] in old_files or e in new_ids]
    if new_ids[:len(kept)] != kept or len(new_ids) < len(kept):
        return [f"{reg.IDS_FILE}: existing entries may not be changed or removed ({rule})"]
    return []


def check_transition(old: State, new: State) -> list:
    if new.ids_text is None:
        return [f"{reg.IDS_FILE} was deleted (B2)"] if old.ids_text is not None else []
    if old.ids_text is None:  # bootstrap: versioning is introduced by this commit
        have = _ids_in(new)
        return [f"{p}: requirement {i} is missing after the migration (B1)"
                for i, p in _ids_in(old).items() if i not in have]
    errs = []
    if old.document_text is not None and new.document_text is None:
        errs.append(f"{DOC_FILE} was deleted (D1)")
    try:
        old_ids, new_ids = reg.load_ids(old.ids_text), reg.load_ids(new.ids_text)
        old_sets, new_sets = reg.load_sets(old.sets_text), reg.load_sets(new.sets_text)
    except ValueError as exc:
        return [str(exc)]
    # B2 append-only id registry
    # Exception: entries that already had no requirement file in the old state (A4 was
    # violated) may be dropped to repair the registry; nothing else may be removed.
    errs += _check_registry_append(old, old_ids, new_ids, "B2")
    # B8 set transitions
    if [s["tag"] for s in new_sets[:len(old_sets)]] != [s["tag"] for s in old_sets]:
        errs.append(f"{reg.SETS_FILE}: existing sets may not be removed or reordered (B8)")
        return errs
    added = new_sets[len(old_sets):]
    if len(added) > 1:
        errs.append(f"{reg.SETS_FILE}: a commit may add at most one set (B7)")
    newtags = {s["tag"]: s for s in new_sets}
    for o in old_sets:
        n = newtags[o["tag"]]
        for k in ("tag", "release", "released_commit", "closed", "members"):
            if o[k] != n[k]:
                errs.append(f"{reg.SETS_FILE}: set {o['tag']} field {k} is locked (B8)")
        for k in ("superseded_by", "invalidated", "invalidated_by"):
            if o[k] is not None and o[k] != n[k]:
                errs.append(f"{reg.SETS_FILE}: set {o['tag']} field {k} is locked once set (B8)")
        ok = {"closed": ("closed", "superseded", "invalidated"),
              "superseded": ("superseded", "invalidated"),
              "invalidated": ("invalidated",)}
        if n["status"] not in ok.get(o["status"], ()):
            errs.append(f"{reg.SETS_FILE}: set {o['tag']} cannot go from {o['status']} to "
                        f"{n['status']} (B8)")
        if o["status"] == "closed" and n["status"] == "superseded" and not added:
            errs.append(f"{reg.SETS_FILE}: a set is only superseded by a newer release (B8)")
    # files
    for p in old.files:
        if p not in new.files:
            errs.append(f"{p}: requirement files may never be deleted, renamed or moved (B1)")
    created = [p for p in new.files if p not in old.files]
    changed = [p for p in new.files if p in old.files and new.files[p] != old.files[p]]
    new_docs, new_paths, nerrs = _docs(new)
    old_docs, _, _ = _docs(old)
    if nerrs:
        return errs  # check_tree already reports the file problems
    old_by_path = {}
    for p in old.files:
        m = PATH_RE.match(p)
        if m and m.group(2) in old_docs:
            old_by_path[p] = old_docs[m.group(2)]
    old_id_set = {e["id"] for e in old_ids}
    for p in created:
        d = new_docs.get(PATH_RE.match(p).group(2)) if PATH_RE.match(p) else None
        if d is None:
            continue
        if len(added) == 1:
            errs.append(f"{p}: a baseline commit may not add files (B7)")
            continue
        if d.id in old_id_set:
            errs.append(f"{p}: id {d.id} was issued before, its file must not be recreated (B3)")
        if d.fm["revision"] != 1 or len(d.history) != 1:
            errs.append(f"{p}: new files start at revision 1 with one History entry (B3)")
        if d.fm["requirement_set"] is not None:
            errs.append(f"{p}: new files have no requirement_set (B3)")
    if added:
        errs += _check_baseline(old, new, old_docs, new_docs, old_by_path, new_paths,
                                changed, added[0], old_sets, new_sets, old_ids, new_ids)
        return errs
    old_tags = {s["tag"]: s for s in old_sets}
    for p in changed:
        o, n = old_by_path.get(p), new_docs.get(PATH_RE.match(p).group(2))
        if o is None or n is None:
            continue
        errs += _check_change(p, o, n)
        if len(n.history) == len(o.history) + 1 and n.history[-1]["set"] is not None:
            errs.append(f"{p}: only the release workflow may set requirement_set (B5)")
        rs = o.fm["requirement_set"]
        if rs and old_tags.get(rs, {}).get("status") == "closed":
            if newtags[rs]["status"] != "invalidated":
                errs.append(f"{p}: changing a member of closed set {rs} requires marking the "
                            f"set invalidated in the same commit (B6)")
    return errs


def _check_change(p, o, n):
    errs = []
    for k in ("id", "created", "level"):
        if o.fm[k] != n.fm[k]:
            errs.append(f"{p}: field {k} is locked and cannot change (B4)")
    if n.fm["revision"] != o.fm["revision"] + 1:
        errs.append(f"{p}: revision must go from {o.fm['revision']} to "
                    f"{o.fm['revision'] + 1} when a requirement changes (B4)")
    if len(n.history) != len(o.history) + 1:
        errs.append(f"{p}: exactly one new History entry must be appended (B4)")
    elif n.history[:len(o.history)] != o.history:
        errs.append(f"{p}: old History entries and snapshots may never be edited (B4)")
    return errs


def _check_baseline(old, new, old_docs, new_docs, old_by_path, new_paths, changed, s,
                    old_sets, new_sets, old_ids, new_ids):
    errs, tag = [], s["tag"]
    if new_ids != old_ids:
        errs.append("a baseline commit may not touch the id registry (B7)")
    if s["status"] != "closed":
        errs.append(f"new set {tag} must have status closed (B7)")
    expected = sorted(i for i, d in new_docs.items()
                      if d.fm["set_specific"] and d.fm["status"] not in NOT_IN_SET_STATUSES)
    listed = sorted(m["id"] for m in s["members"] or [])
    if listed != expected:
        errs.append(f"set {tag} members must be exactly all set_specific requirements that "
                    f"are not Withdrawn/Rejected (B7); expected {expected}, got {listed}")
    changed_ids = sorted(PATH_RE.match(p).group(2) for p in changed)
    if changed_ids != expected:
        errs.append(f"a baseline commit must change exactly the member files (B7); "
                    f"changed {changed_ids}")
    revs = {m["id"]: m["revision"] for m in s["members"] or []}
    for p in changed:
        o, n = old_by_path.get(p), new_docs.get(PATH_RE.match(p).group(2))
        if o is None or n is None:
            continue
        errs += _check_change(p, o, n)
        if revs.get(n.id) != n.fm["revision"]:
            errs.append(f"{p}: set {tag} must list {n.id} at revision {n.fm['revision']} (B7)")
        if n.fm["requirement_set"] != tag:
            errs.append(f"{p}: requirement_set must be {tag} (B7)")
        e = n.history[-1]
        if e["set"] != tag or not e["change"].startswith(BASELINE_CHANGE_PREFIX):
            errs.append(f"{p}: baseline History entry must start with "
                        f"'{BASELINE_CHANGE_PREFIX}' and carry the set tag (B7)")
        other = [k for k in o.fm if k not in ("revision", "requirement_set") and o.fm[k] != n.fm[k]]
        if other or o.text != n.text:
            errs.append(f"{p}: a baseline commit may only change revision, requirement_set and "
                        f"History, not content ({', '.join(other) or 'sections'}) (B7)")
        want = dict(o.history[-1]["snapshot"], requirement_set=tag)
        if e["snapshot"] != want:
            errs.append(f"{p}: baseline snapshot must equal the previous snapshot with "
                        "requirement_set set (B7)")
    old_closed = [x["tag"] for x in old_sets if x["status"] == "closed"]
    nt = {x["tag"]: x for x in new_sets}
    for t in old_closed:
        if nt[t]["status"] != "superseded" or nt[t]["superseded_by"] != tag:
            errs.append(f"previous closed set {t} must become superseded by {tag} (B7)")
    return errs


def check_branch(base: State, new: State) -> list:
    """Branch rule (C1): between the merge base and a branch commit people may edit the
    content of requirements freely, but not the frozen data. Exactness (one revision per
    changed requirement, correct snapshots) is the finalize/gate step's job."""
    if new.ids_text is None:
        return [f"{reg.IDS_FILE} was deleted (C1)"] if base.ids_text is not None else []
    if base.ids_text is None:  # bootstrap: versioning is introduced on this branch
        return check_transition(base, new)
    errs = []
    if base.document_text is not None and new.document_text is None:
        errs.append(f"{DOC_FILE} was deleted (D1)")
    try:
        base_ids, new_ids = reg.load_ids(base.ids_text), reg.load_ids(new.ids_text)
        base_sets, new_sets = reg.load_sets(base.sets_text), reg.load_sets(new.sets_text)
    except ValueError as exc:
        return [str(exc)]
    errs += [e.replace("(B2)", "(C1)") for e in _check_registry_append(base, base_ids, new_ids, "C1")]
    # requirement-sets.yaml equals base, except closed -> invalidated (finalize does that)
    if len(new_sets) != len(base_sets) or [s["tag"] for s in new_sets] != [s["tag"] for s in base_sets]:
        errs.append(f"{reg.SETS_FILE}: sets may not be added, removed or reordered on a branch (C1)")
    else:
        for o, n in zip(base_sets, new_sets):
            if o == n:
                continue
            same = all(o[k] == n[k] for k in reg.SET_KEYS
                       if k not in ("status", "invalidated", "invalidated_by"))
            if not (same and o["status"] == "closed" and n["status"] == "invalidated"):
                errs.append(f"{reg.SETS_FILE}: set {o['tag']} may not be edited on a branch; "
                            "only closed -> invalidated is allowed, and finalize writes it (C1)")
    for p in base.files:
        if p not in new.files:
            errs.append(f"{p}: requirement files may never be deleted, renamed or moved (B1)")
    base_docs, _, _ = _docs(base)
    new_docs, new_paths, nerrs = _docs(new, branch=True)
    if nerrs:
        return errs  # check_tree reports the file problems
    base_issued = {e["id"] for e in base_ids}
    new_issued = {e["id"] for e in new_ids}
    for p, text in new.files.items():
        m = PATH_RE.match(p)
        d = new_docs.get(m.group(2)) if m else None
        if d is None:
            continue
        if p in base.files:
            o = base_docs.get(m.group(2))
            if o is None:
                continue
            errs += _check_branch_change(p, o, d)
        else:
            if d.id in base_issued:
                errs.append(f"{p}: id {d.id} was issued before, its file must not be recreated (C1)")
            if d.id not in new_issued:
                errs.append(f"{p}: id {d.id} is not in {reg.IDS_FILE}; allocate ids with "
                            "`reqcheck new` (C1)")
            if d.fm["revision"] != 1 or len(d.history) != 1:
                errs.append(f"{p}: new files start at revision 1 with one History entry (C1)")
            if d.fm["requirement_set"] is not None:
                errs.append(f"{p}: new files have no requirement_set (C1)")
    return errs


def _check_branch_change(p, o, n):
    errs = []
    for k in ("id", "created", "level"):
        if o.fm[k] != n.fm[k]:
            errs.append(f"{p}: field {k} is locked and cannot change (C1)")
    if n.history[:len(o.history)] != o.history:
        errs.append(f"{p}: old History entries and snapshots may never be edited (C1)")
    if len(n.history) > len(o.history) + 1:
        errs.append(f"{p}: at most one History entry may be appended, by finalize (C1)")
    if n.fm["revision"] not in (o.fm["revision"], o.fm["revision"] + 1):
        errs.append(f"{p}: revision is managed by finalize and must stay {o.fm['revision']} "
                    f"(or become {o.fm['revision'] + 1} when finalized) (C1)")
    if n.fm["requirement_set"] not in (o.fm["requirement_set"], None):
        errs.append(f"{p}: requirement_set is managed by the release and finalize steps (C1)")
    return errs
