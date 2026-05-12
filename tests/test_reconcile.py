"""Tests for reconcile: create/update/delete diff logic."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import patch, MagicMock, call
import dedup, index

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}

JIRA_ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "My Epic",
        "issuetype": {"name": "Epic"},
        "assignee": {"displayName": "Tawan Vongsombun"},
        "customfield_10016": 5.0,
        "customfield_10175": None,
        "customfield_10176": None,
        "status": {"name": "In Progress"},
        "parent": None,
    }
}

LARK_RECORD = {
    "record_id": "recABC",
    "fields": {
        "Title": "My Epic",
        "Jira Key": "PROJ-1",
        "Jira URL": "https://test.atlassian.net/browse/PROJ-1",
        "Type": "Epic",
        "Assignee": [{"text": "Tawan"}],
        "R. MD": 5,
        "Jira status": "To Do",
        "Actual start date": None,
        "Actual end date": None,
    }
}


def setup_function():
    dedup._cache.clear()
    index._jira_to_lark.clear()
    index._lark_to_jira.clear()


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_create_missing_lark_record(mock_lark, mock_jira):
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = []
    mock_jira.fetch_all_issues.return_value = [JIRA_ISSUE]
    mock_lark.create_record.return_value = "recNEW"

    import reconcile
    reconcile.run(CFG)

    mock_lark.create_record.assert_called_once()
    fields = mock_lark.create_record.call_args[0][3]
    assert fields["Title"] == "My Epic"
    assert fields["Jira Key"] == "PROJ-1"
    assert fields["Assignee"] == ["Tawan"]
    assert fields["R. MD"] == 5
    assert dedup.is_ours("lark:recNEW")
    assert index._jira_to_lark.get("PROJ-1") == "recNEW"


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_update_stale_status_in_lark(mock_lark, mock_jira):
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = [LARK_RECORD]
    mock_jira.fetch_all_issues.return_value = [JIRA_ISSUE]  # status=In Progress

    import reconcile
    reconcile.run(CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Jira status"] == "In Progress"
    assert dedup.is_ours("lark:recABC")


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_no_update_when_already_in_sync(mock_lark, mock_jira):
    in_sync_record = {**LARK_RECORD, "fields": {**LARK_RECORD["fields"], "Jira status": "In Progress"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = [in_sync_record]
    mock_jira.fetch_all_issues.return_value = [JIRA_ISSUE]

    import reconcile
    reconcile.run(CFG)

    mock_lark.update_record.assert_not_called()


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_delete_orphaned_lark_record(mock_lark, mock_jira):
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = [LARK_RECORD]
    mock_jira.fetch_all_issues.return_value = []  # PROJ-1 gone from Jira

    import reconcile
    reconcile.run(CFG)

    mock_lark.delete_record.assert_called_once()
    deleted_rid = mock_lark.delete_record.call_args[0][3]
    assert deleted_rid == "recABC"
    assert dedup.is_ours("lark:recABC")


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_skip_non_allowed_issue_type(mock_lark, mock_jira):
    subtask = {**JIRA_ISSUE, "fields": {**JIRA_ISSUE["fields"], "issuetype": {"name": "Sub-task"}}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = []
    mock_jira.fetch_all_issues.return_value = [subtask]

    import reconcile
    reconcile.run(CFG)

    mock_lark.create_record.assert_not_called()


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_safety_guard_blocks_mass_delete(mock_lark, mock_jira):
    records = [
        {"record_id": f"rec{i}", "fields": {"Jira Key": f"PROJ-{i}"}}
        for i in range(11)
    ]
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = records
    mock_jira.fetch_all_issues.return_value = []  # all 11 gone

    import reconcile
    reconcile.run(CFG)

    mock_lark.delete_record.assert_not_called()


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_story_points_int_formatting(mock_lark, mock_jira):
    """Lark number field requires numeric value — strings raise NumberFieldConvFail."""
    issue = {**JIRA_ISSUE, "fields": {**JIRA_ISSUE["fields"], "customfield_10016": 3.0}}
    stale_rec = {**LARK_RECORD, "fields": {**LARK_RECORD["fields"],
                                            "Jira status": "In Progress",
                                            "R. MD": 0}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = [stale_rec]
    mock_jira.fetch_all_issues.return_value = [issue]

    import reconcile
    reconcile.run(CFG)

    fields = mock_lark.update_record.call_args[0][4]
    assert fields["R. MD"] == 3
    assert isinstance(fields["R. MD"], int)
