"""The ID registry (id-registry.yaml) and the requirement-set registry."""
from __future__ import annotations

import yaml

from .schema import ID_RE, split_id, yload

IDS_FILE = "id-registry.yaml"
SETS_FILE = "requirement-sets.yaml"

SET_KEYS = ["tag", "status", "release", "released_commit", "closed", "members",
            "superseded_by", "invalidated", "invalidated_by"]
SET_STATUSES = ["closed", "superseded", "invalidated"]

IDS_HEADER = """\
# Every requirement ID ever issued. APPEND-ONLY: never edit or remove an entry.
# Allocate IDs with: python3 -m tools.reqcheck new --level N --title "..."
"""
SETS_HEADER = """\
# Requirement sets. Written only by the release workflow
# (.github/workflows/requirement-set.yml) and by `reqcheck finalize`
# (which marks a set invalidated). Never edit by hand. See RULES.md.
"""


def load_ids(text):
    data = yload(text) if text is not None else None
    if text is None:
        return []
    if not isinstance(data, dict) or list(data) != ["ids"] or not isinstance(data["ids"], list):
        raise ValueError(f"{IDS_FILE} must be a mapping with the single key 'ids'")
    for e in data["ids"]:
        if not (isinstance(e, dict) and list(e) == ["id", "issued"]
                and isinstance(e["id"], str) and ID_RE.match(e["id"])
                and isinstance(e["issued"], str)):
            raise ValueError(f"{IDS_FILE}: bad entry {e!r} (need id and issued)")
    return data["ids"]


def dump_ids(ids) -> str:
    lines = [f"  - {{id: {e['id']}, issued: {e['issued']}}}" for e in ids]
    return IDS_HEADER + "ids:" + ("\n" + "\n".join(lines) if lines else " []") + "\n"


def next_id(ids, level: int) -> str:
    """The next free ID for a level: highest issued number + 1 (never reused)."""
    nums = [split_id(e["id"])[1] for e in ids if split_id(e["id"])[0] == level]
    return f"LP-{level}-{max(nums, default=0) + 1}"


def load_sets(text):
    if text is None:
        return []
    data = yload(text)
    if not isinstance(data, dict) or list(data) != ["sets"] or not isinstance(data["sets"], list):
        raise ValueError(f"{SETS_FILE} must be a mapping with the single key 'sets'")
    for s in data["sets"]:
        if not isinstance(s, dict) or list(s) != SET_KEYS:
            raise ValueError(f"{SETS_FILE}: set entries need exactly the keys {SET_KEYS}")
    return data["sets"]


def dump_sets(sets) -> str:
    body = yaml.safe_dump({"sets": sets}, sort_keys=False, default_flow_style=False,
                          width=10**9, allow_unicode=True)
    return SETS_HEADER + body


def new_set(tag, release, commit, closed, members):
    return {"tag": tag, "status": "closed", "release": release,
            "released_commit": commit, "closed": closed,
            "members": [{"id": i, "revision": r} for i, r in members],
            "superseded_by": None, "invalidated": None, "invalidated_by": None}
