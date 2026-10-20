"""One assignee rule for every Jira->Lark path: a Jira assignee who is
unassigned or not in the project's people list clears the Lark Assignee;
unmapped additionally writes ONE skipped history row naming the person.
Lark->Jira: an unmapped Lark assignee leaves Jira unchanged (+ same warning)."""
import pytest

import config, history, index, jira_handler, lark_handler, projects, reconcile
from tests.conftest import P2_TABLE
from tests.test_unmapped_fields import (  # noqa: F401  (fixtures)
    CFG2, _clean, _full_issue, _p2_record, four, lark_mocks, mock_lark, rec_mocks)


def _warnings():
    return [r for r in history._fallback if "not in people list" in r["description"]]


def _issue(key, assignee):
    issue = _full_issue(key, parent="TWO-1")
    if assignee is None:
        issue["fields"]["assignee"] = None
    else:
        issue["fields"]["assignee"] = {"displayName": assignee}
    return issue


def _rec(rid, key, assignee):
    r = _p2_record(rid, key)
    r["fields"]["Assignee"] = [{"text": assignee}] if assignee else None
    return r


# ── config.lark_assignee_for ────────────────────────────────────────────

def test_lark_assignee_for(four):
    with projects.use(2):
        assert config.lark_assignee_for("Ada Lovelace") == ("Ada", False)
        assert config.lark_assignee_for("Nobody Known") == (None, True)
        assert config.lark_assignee_for(None) == (None, False)
        assert config.lark_assignee_for("") == (None, False)


# ── jira_handler ────────────────────────────────────────────────────────

def _update(mock_lark, issue, to_str, cur):
    with projects.use(2):
        index.add("TWO-5", "rec5")
    mock_lark.get_cached_or_fetch_record.return_value = {"fields": {
        "Title": "S TWO-5", "Assignee": [{"text": cur}] if cur else None}}
    cl = {"items": [{"field": "assignee", "fieldId": "assignee", "toString": to_str}]}
    jira_handler.process("jira:issue_updated", issue, cl, CFG2)


def test_jira_update_unmapped_clears_lark_and_warns_once(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", "Bob Stranger"), "Bob Stranger", cur="Ada")
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": None}
    w = _warnings()
    assert len(w) == 1
    assert w[0]["status"] == "skipped" and w[0]["event"] == "updated"
    assert w[0]["jira_key"] == "TWO-5"
    assert w[0]["description"] == ("Assignee 'Bob Stranger' not in people list "
                                   "— Lark Assignee cleared")


def test_jira_update_unmapped_already_empty_no_write_no_warning(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", "Bob Stranger"), "Bob Stranger", cur=None)
    mock_lark.update_record.assert_not_called()
    assert _warnings() == []


def test_jira_update_unassigned_clears_without_warning(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", None), None, cur="Ada")
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": None}
    assert _warnings() == []


def test_jira_update_mapped_still_sets(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", "Ada Lovelace"), "Ada Lovelace", cur=None)
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": ["Ada"]}
    assert _warnings() == []


def test_jira_create_unmapped_leaves_empty_and_warns(four, mock_lark):
    jira_handler.process("jira:issue_created", _issue("TWO-6", "Bob Stranger"), {}, CFG2)
    assert "Assignee" not in mock_lark.create_record.call_args[0][3]
    w = _warnings()
    assert len(w) == 1 and w[0]["status"] == "skipped" and "Bob Stranger" in w[0]["description"]


def test_jira_create_unassigned_no_warning(four, mock_lark):
    jira_handler.process("jira:issue_created", _issue("TWO-6", None), {}, CFG2)
    assert "Assignee" not in mock_lark.create_record.call_args[0][3]
    assert _warnings() == []


# ── reconcile ───────────────────────────────────────────────────────────

def _sweep(rec_mocks, issue, record):
    ml, mj = rec_mocks
    ml.fetch_all_records.return_value = [record]
    mj.fetch_all_issues.return_value = [issue]
    reconcile.run(CFG2)
    return ml


def test_reconcile_unmapped_clears_lark_and_warns_once(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", "Bob Stranger"), _rec("rec5", "TWO-5", "Ada"))
    assigned = [c[0][4] for c in ml.update_record.call_args_list if "Assignee" in c[0][4]]
    assert assigned == [{**assigned[0]}] and assigned[0]["Assignee"] is None
    assert len(_warnings()) == 1


def test_reconcile_unmapped_already_empty_is_silent(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", "Bob Stranger"), _rec("rec5", "TWO-5", None))
    assert not any("Assignee" in c[0][4] for c in ml.update_record.call_args_list)
    assert _warnings() == []


def test_reconcile_unassigned_clears_no_warning(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", None), _rec("rec5", "TWO-5", "Ada"))
    assert any(c[0][4].get("Assignee", 1) is None for c in ml.update_record.call_args_list)
    assert _warnings() == []


def test_reconcile_mapped_person_still_set(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", "Ada Lovelace"), _rec("rec5", "TWO-5", None))
    assert any(c[0][4].get("Assignee") == ["Ada"] for c in ml.update_record.call_args_list)
    assert _warnings() == []


def test_reconcile_create_unmapped_warns(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-9", "Bob Stranger"), _rec("rec5", "TWO-5", "Ada"))
    assert "Assignee" not in ml.create_record.call_args[0][3]
    assert len(_warnings()) == 1


def test_backfill_unmapped_clears_and_warns_once(four, rec_mocks):
    ml, mj = rec_mocks
    ml.fetch_all_records.return_value = [_rec("rec5", "TWO-5", "Ada")]
    mj.fetch_all_issues.return_value = [_issue("TWO-5", "Bob Stranger")]
    reconcile.backfill(CFG2)
    assert any(c[0][4].get("Assignee", 1) is None for c in ml.update_record.call_args_list)
    assert len(_warnings()) == 1
    # second pass: Lark is now empty -> silent
    ml.update_record.reset_mock()
    ml.fetch_all_records.return_value = [_rec("rec5", "TWO-5", None)]
    history._fallback.clear()
    reconcile.backfill(CFG2)
    assert not any("Assignee" in c[0][4] for c in ml.update_record.call_args_list)
    assert _warnings() == []


# ── lark_handler (Lark -> Jira) ─────────────────────────────────────────

def _lark_update(lark_mocks, assignee):
    ml, mj = lark_mocks
    with projects.use(2):
        index.add("TWO-5", "rec5")
    ml.get_record.return_value = _rec("rec5", "TWO-5", assignee)
    mj.get_issue.return_value = {"fields": {
        "summary": "Old", "duedate": None, config.JF_START: None,
        "assignee": {"accountId": "acc-ada"}, "fixVersions": [],
        "parent": None, "status": {"name": "To Do"}, config.JF_SPRINT: []}}
    lark_handler.process({"action": "record_edited", "record_id": "rec5"}, P2_TABLE, CFG2)
    return mj


def test_lark_update_unmapped_leaves_jira_and_warns(four, lark_mocks):
    mj = _lark_update(lark_mocks, "Zed")
    for c in mj.update_issue.call_args_list:
        assert "assignee" not in c[0][2]
    w = _warnings()
    assert len(w) == 1 and w[0]["direction"] == "lark→jira" and w[0]["status"] == "skipped"
    assert w[0]["jira_key"] == "TWO-5"
    assert w[0]["description"] == ("Assignee 'Zed' not in people list "
                                   "— Jira Assignee unchanged")


def test_lark_update_mapped_person_pushes(four, lark_mocks):
    ml, mj = lark_mocks
    mj.get_account_ids.return_value = {"Ada Lovelace": "acc-ada"}
    mj = _lark_update(lark_mocks, "Ada")
    assert _warnings() == []


def test_lark_create_unmapped_creates_unassigned_and_warns(four, lark_mocks):
    ml, mj = lark_mocks
    ml.get_record.return_value = _rec("recNew", None, "Zed")
    ml.get_record.return_value["fields"]["Jira Key"] = None
    lark_handler.process({"action": "record_added", "record_id": "recNew"}, P2_TABLE, CFG2)
    assert mj.create_issue.call_args[1]["assignee_id"] is None
    assert len(_warnings()) == 1
