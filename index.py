"""Shared in-memory index: jira_key <-> lark record_id."""
from utils import _lark_text
from config import F_JIRA_KEY

_jira_to_lark: dict[str, str] = {}  # jira_key → record_id
_lark_to_jira: dict[str, str] = {}  # record_id → jira_key


def rebuild(records: list) -> None:
    _jira_to_lark.clear()
    _lark_to_jira.clear()
    for rec in records:
        jk = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if jk:
            _jira_to_lark[jk] = rec["record_id"]
            _lark_to_jira[rec["record_id"]] = jk


def add(jira_key: str, record_id: str) -> None:
    _jira_to_lark[jira_key] = record_id
    _lark_to_jira[record_id] = jira_key


def remove_by_jira(jira_key: str) -> None:
    rid = _jira_to_lark.pop(jira_key, None)
    if rid:
        _lark_to_jira.pop(rid, None)


def remove_by_lark(record_id: str) -> None:
    jk = _lark_to_jira.pop(record_id, None)
    if jk:
        _jira_to_lark.pop(jk, None)
