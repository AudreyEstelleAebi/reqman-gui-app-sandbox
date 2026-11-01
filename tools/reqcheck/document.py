"""document.yaml: metadata of the generated requirements document (rule D1)."""
from __future__ import annotations

from .schema import _is_date, yload

DOC_FILE = "document.yaml"

TOP_KEYS = ["document", "authors", "revisions", "purpose", "levels"]
DOC_KEYS = ["project", "title", "header_title", "number", "issue", "released",
            "classification", "author", "reviewed_by", "approved_by", "address", "disclaimer"]
AUTHOR_KEYS = ["name", "initials", "role", "entity"]
REVISION_KEYS = ["issue", "date", "authors", "sections", "change"]
LEVEL_KEYS = ["level", "heading", "intro", "group_by_category"]


def _text(v):
    return isinstance(v, str) and v.strip() != ""


def _keys(m, want, where, errs, optional=()):
    if not isinstance(m, dict):
        errs.append(f"{where} must be a mapping")
        return False
    missing = [k for k in want if k not in m]
    extra = [k for k in m if k not in want and k not in optional]
    if missing:
        errs.append(f"{where}: missing keys {', '.join(missing)}")
    if extra:
        errs.append(f"{where}: unknown keys {', '.join(map(str, extra))}")
    return not missing


def check_document(text, categories_by_level=None) -> list:
    """Validate document.yaml. categories_by_level: {level: set of categories in use}
    or None to skip the usage check."""
    pre = f"{DOC_FILE}: "
    try:
        data = yload(text)
    except Exception as exc:
        return [f"{pre}not valid YAML: {exc} (D1)"]
    errs = []
    if not _keys(data, TOP_KEYS, "top level", errs):
        return [pre + e + " (D1)" for e in errs]
    d = data["document"]
    if _keys(d, DOC_KEYS, "document", errs):
        for k in DOC_KEYS:
            if k == "address":
                if not (isinstance(d[k], list) and d[k] and all(_text(x) for x in d[k])):
                    errs.append("document.address must be a non-empty list of lines")
            elif k == "released":
                if not _is_date(d[k]):
                    errs.append("document.released must be an ISO date (YYYY-MM-DD)")
            elif not _text(d[k]):
                errs.append(f"document.{k} must be a non-empty string"
                            + (' (quote it, e.g. "003")' if k == "issue" else ""))
    initials, authors = set(), data["authors"]
    if not (isinstance(authors, list) and authors):
        errs.append("authors must be a non-empty list")
    else:
        for i, a in enumerate(authors, 1):
            if _keys(a, AUTHOR_KEYS, f"authors[{i}]", errs):
                for k in AUTHOR_KEYS:
                    if not _text(a[k]):
                        errs.append(f"authors[{i}].{k} must be a non-empty string")
                if a["initials"] in initials:
                    errs.append(f"authors[{i}]: initials {a['initials']} used twice")
                initials.add(a["initials"])
    revs = data["revisions"]
    if not (isinstance(revs, list) and revs):
        errs.append("revisions must be a non-empty list")
    else:
        for i, r in enumerate(revs, 1):
            w = f"revisions[{i}]"
            if not _keys(r, REVISION_KEYS, w, errs):
                continue
            for k in ("issue", "sections", "change"):
                if not _text(r[k]):
                    errs.append(f"{w}.{k} must be a non-empty string")
            if not _is_date(r["date"]):
                errs.append(f"{w}.date must be an ISO date (YYYY-MM-DD)")
            if not (isinstance(r["authors"], list) and r["authors"]):
                errs.append(f"{w}.authors must be a non-empty list of author initials")
            else:
                for x in r["authors"]:
                    if x not in initials:
                        errs.append(f"{w}: author initials {x!r} are not in authors")
    if isinstance(revs, list) and isinstance(d, dict) and revs and isinstance(revs[-1], dict) \
            and _text(d.get("issue")) and revs[-1].get("issue") != d["issue"]:
        errs.append("document.issue must equal the issue of the last revision")
    if not _text(data["purpose"]):
        errs.append("purpose must be non-empty text")
    levels = data["levels"]
    if not (isinstance(levels, list) and levels):
        errs.append("levels must be a non-empty list")
    else:
        seen = set()
        for i, lv in enumerate(levels, 1):
            w = f"levels[{i}]"
            grouped = isinstance(lv, dict) and lv.get("group_by_category") is True
            if not _keys(lv, LEVEL_KEYS + (["categories"] if grouped else []), w, errs,
                         optional=["categories"]):
                continue
            n = lv["level"]
            if not (isinstance(n, int) and not isinstance(n, bool) and n >= 0) or n in seen:
                errs.append(f"{w}.level must be a unique integer >= 0")
            seen.add(n)
            for k in ("heading", "intro"):
                if not _text(lv[k]):
                    errs.append(f"{w}.{k} must be a non-empty string")
            if not isinstance(lv["group_by_category"], bool):
                errs.append(f"{w}.group_by_category must be true or false")
            elif grouped:
                cats = lv["categories"]
                if not (isinstance(cats, list) and cats and all(_text(c) for c in cats)):
                    errs.append(f"{w}.categories must be a non-empty list of names")
                elif len(set(cats)) != len(cats):
                    errs.append(f"{w}.categories must not repeat a category")
                elif categories_by_level is not None:
                    for c in cats:
                        if c not in categories_by_level.get(n, set()):
                            errs.append(f"{w}: category {c!r} is not used by any level {n} "
                                        "requirement")
            elif "categories" in lv:
                errs.append(f"{w}.categories only applies when group_by_category is true")
    return [pre + e + " (D1)" for e in errs]
