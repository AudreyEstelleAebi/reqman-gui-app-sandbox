"""Parse the `## Requirement changes` block of a pull request description.

    ## Requirement changes
    Author: F. A. Dannert
    Change: Clarified null-depth wording.
    Reason: Review feedback from the science team.
    - LP-1-4: Change: Tightened null depth to 1x10-6 | Reason: Updated science case

The section starts at the heading and ends at the next `## ` heading or the end of
the text. `Author`, `Change` and `Reason` are defaults for every requirement; a
`- <id>: Change: ... | Reason: ...` line overrides them for one requirement (either
part may be left out). Whether Author/Change/Reason are actually needed is decided
by finalize: a pull request that changes no requirement needs no block at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import ID_RE

HEADING_RE = re.compile(r"^##[ \t]+Requirement changes[ \t]*$", re.I)
NEXT_HEADING_RE = re.compile(r"^##[ \t]")
KEY_RE = re.compile(r"^(Author|Change|Reason):[ \t]*(.*?)[ \t]*$", re.I)
ID_LINE_RE = re.compile(r"^[-*][ \t]+(LP-\d+-\d+)[ \t]*:[ \t]*(.*?)[ \t]*$")
SEGMENT_SPLIT_RE = re.compile(r"[ \t]+\|[ \t]+(?=(?:Change|Reason)[ \t]*:)", re.I)
SEGMENT_RE = re.compile(r"^(Change|Reason)[ \t]*:[ \t]*(.*?)[ \t]*$", re.I | re.S)


@dataclass
class PRBlock:
    author: str | None = None
    change: str | None = None
    reason: str | None = None
    per_id: dict = field(default_factory=dict)  # id -> {"change": str|None, "reason": str|None}


def parse_pr_block(text):
    """-> PRBlock, or None when the description has no `## Requirement changes` section.
    Raises ValueError for lines that cannot be understood (typos must not be ignored)."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = next((i for i, ln in enumerate(lines) if HEADING_RE.match(ln.rstrip())), None)
    if start is None:
        return None
    body = []
    for ln in lines[start + 1:]:
        if NEXT_HEADING_RE.match(ln):
            break
        body.append(ln)
    block, errs = PRBlock(), []
    body = re.sub(r"<!--.*?-->", "", "\n".join(body), flags=re.S).split("\n")
    for ln in body:
        s = ln.strip()
        if not s:
            continue
        m = ID_LINE_RE.match(s)
        if m:
            rid, rest = m.group(1), m.group(2)
            if not ID_RE.match(rid):
                errs.append(f"bad requirement id {rid!r}")
                continue
            if rid in block.per_id:
                errs.append(f"{rid} is listed twice")
                continue
            entry = {"change": None, "reason": None}
            for seg in SEGMENT_SPLIT_RE.split(rest):
                sm = SEGMENT_RE.match(seg.strip())
                if not sm or not sm.group(2):
                    errs.append(f"{rid}: expected `Change: ... | Reason: ...`, got {seg!r}")
                    continue
                key = sm.group(1).lower()
                if entry[key] is not None:
                    errs.append(f"{rid}: {key} given twice")
                entry[key] = sm.group(2)
            if entry["change"] is None and entry["reason"] is None:
                errs.append(f"{rid}: the line needs a Change and/or a Reason")
            block.per_id[rid] = entry
            continue
        m = KEY_RE.match(s)
        if m:
            key = m.group(1).lower()
            if not m.group(2):
                errs.append(f"{m.group(1)} is empty")
            elif getattr(block, key) is not None:
                errs.append(f"{m.group(1)} is given twice")
            else:
                setattr(block, key, m.group(2))
            continue
        if s[0] in "-*":
            errs.append(f"cannot understand the line {s!r} (expected `- LP-x-y: Change: ... | Reason: ...`)")
        # any other line is free text and ignored
    if errs:
        raise ValueError("PR description, section '## Requirement changes': " + "; ".join(errs))
    return block
