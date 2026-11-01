"""Parse, validate and render requirement files (the one and only template)."""
from __future__ import annotations

import datetime
import difflib
import re
from dataclasses import dataclass

import yaml

FM_KEYS = [
    "id", "level", "title", "status", "priority", "category", "verification",
    "parents", "owner", "created", "revision", "set_specific", "requirement_set",
    "tags",
]
SNAP_KEYS = [
    "title", "status", "priority", "category", "verification", "parents", "owner",
    "set_specific", "requirement_set", "tags", "statement", "rationale", "notes",
]
# Snapshots written before `parent` became `parents` keep the old key.
LEGACY_SNAP_KEYS = [("parent" if k == "parents" else k) for k in SNAP_KEYS]
TEXT_KEYS = ["statement", "rationale", "notes"]
SECTIONS = ["Statement", "Rationale", "Notes", "History"]
STATUSES = ["Draft", "Proposed", "Approved", "Verified", "Rejected", "Withdrawn"]
PRIORITIES = ["High", "Medium", "Low"]
VERIFICATIONS = ["Test", "Analysis", "Inspection", "Demonstration"]
ID_RE = re.compile(r"^LP-(0|[1-9]\d*)-([1-9]\d*)$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BASELINE_CHANGE_PREFIX = "Baselined into requirement set"

_BIG = 10**9


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps ISO dates as plain strings."""


_Loader.yaml_implicit_resolvers = {
    k: [(t, r) for t, r in v if t != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def yload(text):
    return yaml.load(text, Loader=_Loader)


def split_id(req_id: str):
    m = ID_RE.match(req_id)
    return (int(m.group(1)), int(m.group(2))) if m else None


def scalar(v) -> str:
    """Render one value the way the template wants it (empty for None)."""
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        out = yaml.safe_dump({"k": v}, default_flow_style=True, width=_BIG,
                             allow_unicode=True).strip()
        return out[len("{k: "):-1]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v  # ISO dates stay plain (our loader reads them back as strings)
    out = yaml.safe_dump({"k": v}, default_flow_style=False, width=_BIG,
                         allow_unicode=True).rstrip("\n")
    return out[len("k: "):]


def _line(k, v) -> str:
    s = scalar(v)
    return f"{k}: {s}" if s != "" else f"{k}:"


def render_fm(fm: dict) -> str:
    return "\n".join(["---"] + [_line(k, fm[k]) for k in FM_KEYS] + ["---"])


def render_snapshot(s: dict) -> str:
    lines = []
    for k in (LEGACY_SNAP_KEYS if "parent" in s else SNAP_KEYS):
        v = s[k]
        if k in TEXT_KEYS and "\n" in v:
            lines.append(f"{k}: |-")
            lines += [("  " + ln) if ln else "" for ln in v.split("\n")]
        else:
            lines.append(_line(k, v))
    return "\n".join(lines)


def render_entry(e: dict) -> str:
    n = e["revision"]
    return (
        f"### Revision {n}\n"
        f"- Date: {e['date']}\n"
        f"- Author: {e['author']}\n"
        f"- Requirement set: {e['set'] if e['set'] else 'none'}\n"
        f"- Change: {e['change']}\n"
        f"- Reason: {e['reason']}\n"
        f"\n"
        f"<details><summary>Snapshot of revision {n}</summary>\n"
        f"\n"
        f"```yaml\n{render_snapshot(e['snapshot'])}\n```\n"
        f"\n"
        f"</details>\n"
    )


@dataclass
class Doc:
    fm: dict
    text: dict      # statement / rationale / notes
    history: list   # list of entry dicts

    @property
    def id(self):
        return self.fm["id"]


def render(doc: Doc) -> str:
    t = doc.text
    return (
        render_fm(doc.fm) + "\n\n"
        f"## Statement\n\n{t['statement']}\n\n"
        f"## Rationale\n\n{t['rationale']}\n\n"
        f"## Notes\n\n{t['notes']}\n\n"
        "## History\n\n" + "\n".join(render_entry(e) for e in doc.history)
    )


def current_snapshot(doc: Doc) -> dict:
    s = {k: doc.fm[k] for k in SNAP_KEYS if k not in TEXT_KEYS}
    s.update({k: doc.text[k] for k in TEXT_KEYS})
    return {k: s[k] for k in SNAP_KEYS}


_ENTRY_RE = re.compile(
    r"### Revision (\d+)\n- Date: (.*)\n- Author: (.*)\n- Requirement set: (.*)\n"
    r"- Change: (.*)\n- Reason: (.*)\n\n"
    r"<details><summary>Snapshot of revision \1</summary>\n\n```yaml\n(.*?)\n```\n\n</details>\n",
    re.S,
)


def _parse(text: str) -> Doc:
    m = re.match(r"---\n(.*?)\n---\n\n", text, re.S)
    if not m:
        raise ValueError("missing or malformed frontmatter")
    fm = yload(m.group(1))
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    parts = re.split(r"^## (Statement|Rationale|Notes|History)\n\n", text[m.end():],
                     flags=re.M)
    if parts[0] != "" or parts[1::2] != SECTIONS:
        raise ValueError("sections must be exactly: " + ", ".join(SECTIONS))
    bodies = dict(zip(parts[1::2], parts[2::2]))
    sect = {}
    for key, name in zip(TEXT_KEYS, SECTIONS):
        body = bodies[name]
        if not body.endswith("\n\n"):
            raise ValueError(f"section {name} is not followed by a blank line")
        sect[key] = body[:-2]
    hist, pos, body = [], 0, bodies["History"]
    while pos < len(body):
        em = _ENTRY_RE.match(body, pos)
        if not em:
            raise ValueError("History entry does not follow the template (near char "
                             f"{pos} of the History section)")
        snap = yload(em.group(7))
        if not isinstance(snap, dict):
            raise ValueError(f"snapshot of revision {em.group(1)} is not a mapping")
        hist.append({
            "revision": int(em.group(1)), "date": em.group(2), "author": em.group(3),
            "set": None if em.group(4) == "none" else em.group(4),
            "change": em.group(5), "reason": em.group(6), "snapshot": snap,
        })
        pos = em.end()
        if pos < len(body):
            if body[pos] != "\n":
                raise ValueError("History entries must be separated by one blank line")
            pos += 1
    return Doc(fm, sect, hist)


def parse(text: str) -> Doc:
    """Parse a file and require that it matches the template byte for byte."""
    if "\r" in text:
        raise ValueError("CRLF line endings are not allowed")
    try:
        doc = _parse(text)
        rendered = render(doc)
    except ValueError:
        raise
    except Exception as exc:  # malformed YAML, missing keys, ...
        raise ValueError(f"cannot parse file: {exc}") from exc
    if rendered != text:
        a, b = text.splitlines(), rendered.splitlines()
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else "<end of file>"
            y = b[i] if i < len(b) else "<end of file>"
            if x != y:
                raise ValueError(
                    f"does not match the template exactly (line {i + 1}: found {x!r}, "
                    f"template expects {y!r})")
        raise ValueError("does not match the template exactly (trailing whitespace?)")
    return doc


def _is_date(s) -> bool:
    try:
        return isinstance(s, str) and datetime.date.fromisoformat(s).isoformat() == s
    except ValueError:
        return False


def _one_line(v) -> bool:
    return isinstance(v, str) and v.strip() == v and v != "" and "\n" not in v


def validate_doc(doc: Doc, check_last_snapshot: bool = True) -> list:
    """Single-file rules (A1, A6). Returns a list of error strings."""
    e = []
    fm = doc.fm
    if list(fm) != FM_KEYS:
        return [f"frontmatter keys must be exactly, in order: {', '.join(FM_KEYS)}"]
    ids = split_id(fm["id"]) if isinstance(fm["id"], str) else None
    if not ids:
        e.append("id must look like LP-<level>-<number>")
    elif fm["level"] != ids[0] or isinstance(fm["level"], bool):
        e.append("level must equal the level inside the id")
    for k in ("title", "category"):
        if not _one_line(fm[k]):
            e.append(f"{k} must be a non-empty single line without surrounding spaces")
    if fm["owner"] is not None and not _one_line(fm["owner"]):
        e.append("owner must be empty or a single line without surrounding spaces")
    if fm["status"] not in STATUSES:
        e.append(f"status must be one of {STATUSES}")
    if fm["priority"] not in PRIORITIES:
        e.append(f"priority must be one of {PRIORITIES}")
    if fm["verification"] is not None and fm["verification"] not in VERIFICATIONS:
        e.append(f"verification must be empty or one of {VERIFICATIONS}")
    ps = fm["parents"]
    if not (isinstance(ps, list) and all(isinstance(x, str) and split_id(x) for x in ps)):
        e.append("parents must be a list of requirement ids, e.g. [LP-0-1, LP-0-4] or []")
    else:
        if len(set(ps)) != len(ps):
            e.append("parents must not list an id twice")
        if fm["id"] in ps:
            e.append("a requirement cannot be its own parent")
    if not _is_date(fm["created"]):
        e.append("created must be an ISO date (YYYY-MM-DD)")
    if not (isinstance(fm["revision"], int) and not isinstance(fm["revision"], bool)
            and fm["revision"] >= 1):
        e.append("revision must be an integer >= 1")
    if not isinstance(fm["set_specific"], bool):
        e.append("set_specific must be true or false")
    if fm["requirement_set"] is not None and not _one_line(fm["requirement_set"]):
        e.append("requirement_set must be empty or a set tag")
    if not (isinstance(fm["tags"], list)
            and all(isinstance(t, str) and TAG_RE.match(t) for t in fm["tags"])):
        e.append("tags must be a list of lowercase slugs")
    for k in TEXT_KEYS:
        v = doc.text[k]
        if not v.strip() or v != v.strip():
            e.append(f"section {k.capitalize()} must be non-empty and trimmed")
        if "```" in v or re.search(r"^(## |### Revision )", v, re.M):
            e.append(f"section {k.capitalize()} must not contain code fences or "
                     "headings that collide with the template")
    # history (A6)
    h = doc.history
    if isinstance(fm["revision"], int) and len(h) != fm["revision"]:
        e.append(f"revision is {fm['revision']} but History has {len(h)} entries")
    prev = None
    legacy_ok = True  # legacy `parent` snapshots may only precede `parents` ones
    for i, en in enumerate(h, 1):
        where = f"History revision {i}"
        if en["revision"] != i:
            e.append(f"{where}: entries must be numbered 1..N without gaps")
        if not _is_date(en["date"]):
            e.append(f"{where}: Date must be an ISO date")
        elif prev and en["date"] < prev:
            e.append(f"{where}: dates must not go backwards")
        prev = en["date"] if _is_date(en["date"]) else prev
        for k in ("author", "change", "reason"):
            if not _one_line(en[k]):
                e.append(f"{where}: {k} must be a non-empty single line")
        sn = en["snapshot"]
        if list(sn) == LEGACY_SNAP_KEYS and legacy_ok:
            pass
        elif list(sn) != SNAP_KEYS:
            e.append(f"{where}: snapshot keys must be exactly, in order: "
                     f"{', '.join(SNAP_KEYS)} (older snapshots may use `parent` "
                     "instead of `parents`)")
        else:
            legacy_ok = False
        if list(sn) not in (SNAP_KEYS, LEGACY_SNAP_KEYS):
            pass
        elif (en["set"] or None) != sn["requirement_set"]:
            e.append(f"{where}: 'Requirement set' must equal the snapshot's requirement_set")
    if h and _is_date(h[0]["date"]) and _is_date(fm["created"]) and h[0]["date"] != fm["created"]:
        e.append("History revision 1 must be dated `created`")
    if check_last_snapshot and h:
        if h[-1]["snapshot"] != current_snapshot(doc):
            diff = [k for k in SNAP_KEYS
                    if h[-1]["snapshot"].get(k) != current_snapshot(doc)[k]]
            e.append("the last History snapshot must equal the current content "
                     f"(differs in: {', '.join(diff)}); on a branch only edit the content, finalize records the revision")
        if h[-1]["set"] != fm["requirement_set"]:
            e.append("requirement_set must equal the last History entry's set")
    return e


def new_doc(req_id, title, owner, created, *, status="Draft", priority="Medium",
            category="TBD", verification=None, parents=None, set_specific=True,
            tags=None, statement="TBD", rationale="TBD", notes="None",
            change="Initial version.", reason="New requirement.", author=None) -> Doc:
    level = split_id(req_id)[0]
    fm = {
        "id": req_id, "level": level, "title": title, "status": status,
        "priority": priority, "category": category, "verification": verification,
        "parents": list(parents or []), "owner": owner, "created": created, "revision": 1,
        "set_specific": set_specific, "requirement_set": None, "tags": list(tags or []),
    }
    doc = Doc(fm, {"statement": statement, "rationale": rationale, "notes": notes}, [])
    doc.history.append({
        "revision": 1, "date": created, "author": author or owner, "set": None,
        "change": change, "reason": reason, "snapshot": current_snapshot(doc),
    })
    return doc


def render_template() -> str:
    """The annotated TEMPLATE.md (checked against the repository file by tests)."""
    return f"""\
# Requirement file template

Do not copy this by hand. Create files with `python3 -m tools.reqcheck new`; on
a branch edit only the content. Revisions and History are written by the
finalize GitHub Action when the PR is merged (see RULES.md). This page
documents the exact layout that the checker enforces. Real files are
`requirements/level-<level>/<id>.md`.

```markdown
---
id: LP-1-4                  # LOCKED forever. Format LP-<level>-<number>
level: 1                    # equals the level inside the id
title: Short name           # one line
status: Draft               # {" | ".join(STATUSES)}
priority: High              # {" | ".join(PRIORITIES)}
category: Some Category     # one line
verification:               # empty or {" | ".join(VERIFICATIONS)}
parents: [LP-0-1, LP-0-4]   # list of ids of existing requirements, [] for none
owner: F. Dannert
created: 2026-09-10         # LOCKED forever. ISO date
revision: 1                 # number of History entries; +1 for every change
set_specific: true          # true = included in sets created by a release
requirement_set:            # written only by the release workflow
tags: [tag-one, tag-two]    # lowercase slugs
---

## Statement

The "shall" text of the requirement.

## Rationale

Why the requirement exists, or TBD.

## Notes

Assumptions, references, open issues, or None.

## History

### Revision 1
- Date: 2026-09-10
- Author: F. Dannert
- Requirement set: none
- Change: One line: what changed.
- Reason: One line: why it changed.

<details><summary>Snapshot of revision 1</summary>

```yaml
title: Short name
status: Draft
priority: High
category: Some Category
verification:
parents: [LP-0-1, LP-0-4]
owner: F. Dannert
set_specific: true
requirement_set:
tags: [tag-one, tag-two]
statement: |-
  Every snapshot repeats ALL mutable fields and the full
  Statement, Rationale and Notes text as they were at that revision.
rationale: TBD
notes: None
```

</details>
```

Rules of the layout:

- Frontmatter keys appear in exactly the order above, with no extra keys.
- Sections appear in the order Statement, Rationale, Notes, History.
- `## History` has one `### Revision N` entry per revision, oldest first,
  numbered 1..N. Entries and their snapshots are never edited afterwards.
- The newest snapshot equals the current frontmatter and sections.
- Snapshots written before `parent` became `parents` keep the old `parent` key;
  every newer snapshot uses `parents`.
- `Requirement set` in an entry is `none` or a set tag and equals the
  snapshot's `requirement_set`.
"""
