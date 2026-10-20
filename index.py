"""Shared in-memory index: jira_key <-> lark record_id, one pair of maps per
project. `index._jira_to_lark` / `index._lark_to_jira` resolve to the
current project's maps (see projects.use)."""
import field_mappings
import projects
from utils import _lark_text

# project id -> (jira_key → record_id, record_id → jira_key)
_maps: "dict[int, tuple[dict, dict]]" = {}


def _for(pid: "int | None" = None) -> "tuple[dict, dict]":
    return _maps.setdefault(projects.current_id() if pid is None else pid, ({}, {}))


# Reads only: assigning or monkeypatch.setattr-ing index._jira_to_lark would
# create a real module attribute that shadows this per-project lookup.
def __getattr__(name: str):
    if name == "_jira_to_lark":
        return _for()[0]
    if name == "_lark_to_jira":
        return _for()[1]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def count(pid: int) -> int:
    return len(_for(pid)[0])


def rebuild(records: list) -> None:
    j2l, l2j = _for()
    j2l.clear()
    l2j.clear()
    key_col = field_mappings.F_JIRA_KEY
    for rec in records:
        jk = _lark_text(rec["fields"].get(key_col))
        if jk:
            j2l[jk] = rec["record_id"]
            l2j[rec["record_id"]] = jk


def add(jira_key: str, record_id: str) -> None:
    j2l, l2j = _for()
    j2l[jira_key] = record_id
    l2j[record_id] = jira_key


def remove_by_jira(jira_key: str) -> None:
    j2l, l2j = _for()
    rid = j2l.pop(jira_key, None)
    if rid:
        l2j.pop(rid, None)


def remove_by_lark(record_id: str) -> None:
    j2l, l2j = _for()
    jk = l2j.pop(record_id, None)
    if jk:
        j2l.pop(jk, None)
