"""Regression: update-path echo suppression breaks a bidirectional date
conflict ping-pong (the 2026-05-19 VR-272 loop). Value-comparison alone
cannot converge a concurrent conflict; date_echo_key marks the value we
just propagated so the mirrored handler skips the echo, while a genuinely
different new value is still propagated."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import patch
import dedup, index
from utils import _jira_date_to_lark_ts

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}

ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "S", "issuetype": {"name": "Story"}, "assignee": None,
        "customfield_10016": None, "customfield_10175": None,
        "customfield_10176": None, "status": {"name": "To Do"}, "parent": None,
    },
}


def setup_function():
    dedup._cache.clear()
    index._jira_to_lark.clear()
    index._lark_to_jira.clear()


def test_date_echo_key_format():
    assert dedup.date_echo_key("PROJ-1", "end", "2026-06-22") == \
        "dateecho:PROJ-1:end:2026-06-22"


@patch("jira_handler.lark_api")
def test_jira_handler_suppresses_echoed_duedate(mock_lark):
    """Lark→Jira just wrote due=2026-06-22 (marked). The Jira webhook echo
    must NOT be propagated back to Lark even though Lark's stored value
    differs (so value-comparison alone would have written)."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {
        "fields": {"Timeline - End": _jira_date_to_lark_ts("2026-01-01")}}
    dedup.mark(dedup.date_echo_key("PROJ-1", "end", "2026-06-22"))
    changelog = {"items": [{"field": "duedate", "to": "2026-06-22",
                            "toString": "2026-06-22 00:00:00.0"}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    mock_lark.update_record.assert_not_called()


@patch("jira_handler.lark_api")
def test_jira_handler_marks_after_writing_lark_date(mock_lark):
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {}}
    changelog = {"items": [{"field": "duedate", "to": "2026-06-22",
                            "toString": "2026-06-22 00:00:00.0"}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    mock_lark.update_record.assert_called_once()
    assert dedup.is_ours(dedup.date_echo_key("PROJ-1", "end", "2026-06-22"))


@patch("jira_handler.lark_api")
def test_jira_handler_does_not_suppress_a_different_date(mock_lark):
    """A genuinely new value (not the marked echo) must still propagate."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {}}
    dedup.mark(dedup.date_echo_key("PROJ-1", "end", "2026-06-22"))
    changelog = {"items": [{"field": "duedate", "to": "2026-06-30",
                            "toString": "2026-06-30 00:00:00.0"}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Timeline - End"] == _jira_date_to_lark_ts("2026-06-30")


def _lrec(end_date):
    return {"record_id": "rec002", "fields": {
        "Title": "T", "Type": "Story", "Jira Key": "PROJ-5", "Assignee": None,
        "Timeline - Start": None,
        "Timeline - End": _jira_date_to_lark_ts(end_date),
        "Parent items": []}}


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_lark_handler_suppresses_echoed_end(mock_lark, mock_jira):
    """Jira→Lark just wrote End=2026-06-22 (marked). The Lark webhook echo
    must NOT be propagated back to Jira even though Jira's duedate differs."""
    index._lark_to_jira["rec002"] = "PROJ-5"
    index._jira_to_lark["PROJ-5"] = "rec002"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = _lrec("2026-06-22")
    mock_jira.get_issue.return_value = {"fields": {
        "summary": "T", "duedate": "2026-01-01", "customfield_10015": None}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None
    dedup.mark(dedup.date_echo_key("PROJ-5", "end", "2026-06-22"))
    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "rec002"},
                         "tbl", CFG)
    if mock_jira.update_issue.called:
        assert "duedate" not in mock_jira.update_issue.call_args[0][2]


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_lark_handler_marks_after_writing_jira_duedate(mock_lark, mock_jira):
    index._lark_to_jira["rec002"] = "PROJ-5"
    index._jira_to_lark["PROJ-5"] = "rec002"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = _lrec("2026-06-22")
    mock_jira.get_issue.return_value = {"fields": {
        "summary": "T", "duedate": "2026-01-01", "customfield_10015": None}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None
    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "rec002"},
                         "tbl", CFG)
    mock_jira.update_issue.assert_called_once()
    assert "duedate" in mock_jira.update_issue.call_args[0][2]
    assert dedup.is_ours(dedup.date_echo_key("PROJ-5", "end", "2026-06-22"))


@patch("jira_handler.lark_api")
@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_full_two_cycle_converges(mock_lh_lark, mock_lh_jira, mock_jh_lark):
    """End-to-end: a Lark End edit → Jira write → Jira weblook echo must
    NOT write Lark again (loop broken in one hop)."""
    index._lark_to_jira["rec002"] = "PROJ-1"
    index._jira_to_lark["PROJ-1"] = "rec002"
    # Hop 1: Lark End=2026-06-22 → lark_handler writes Jira duedate + marks echo
    mock_lh_lark.get_token.return_value = "tok"
    mock_lh_lark.get_record.return_value = _lrec("2026-06-22")
    mock_lh_lark.get_record.return_value["fields"]["Jira Key"] = "PROJ-1"
    mock_lh_jira.get_issue.return_value = {"fields": {
        "summary": "T", "duedate": "2026-01-01", "customfield_10015": None}}
    mock_lh_jira.get_account_ids.return_value = {}
    mock_lh_jira.get_project_versions.return_value = []
    mock_lh_jira.get_board_id.return_value = None
    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "rec002"},
                         "tbl", CFG)
    assert mock_lh_jira.update_issue.called
    # Hop 2: the Jira webhook echo for that same duedate must be suppressed
    mock_jh_lark.get_token.return_value = "tok"
    mock_jh_lark.get_record.return_value = {
        "fields": {"Timeline - End": _jira_date_to_lark_ts("2026-01-01")}}
    changelog = {"items": [{"field": "duedate", "to": "2026-06-22",
                            "toString": "2026-06-22 00:00:00.0"}]}
    import jira_handler
    jira_handler.process("jira:issue_updated",
                         {"key": "PROJ-1", "fields": ISSUE["fields"]},
                         changelog, CFG)
    mock_jh_lark.update_record.assert_not_called()
