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
    assert dedup.is_ours("lark_delete:recABC")


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


@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_no_phantom_write_when_lark_number_is_string(mock_lark, mock_jira):
    """Lark's bitable API returns Number fields as STRINGS ('5'), not ints.

    The value-compare must treat '5' == 5 so reconcile doesn't re-write R. MD
    on a record that's already in sync. Before the fix, the old
    `isinstance(int,float) else None` guard coerced '5' -> None, so 5 != None
    re-wrote R. MD on every record with story points, every 6 h sweep — the
    dominant Lark API-quota drain (~480 update_record/day). Regression guard
    for CHANGELOG 2026-06-08. NB: a string current value is exactly what the
    live API returns; the other reconcile tests use an int and so never caught
    this."""
    in_sync = {**LARK_RECORD, "fields": {**LARK_RECORD["fields"],
                                         "Jira status": "In Progress",
                                         "R. MD": "5"}}  # STRING, as the API returns
    mock_lark.get_token.return_value = "tok"
    mock_lark.fetch_all_records.return_value = [in_sync]
    mock_jira.fetch_all_issues.return_value = [JIRA_ISSUE]  # customfield_10016 = 5.0

    import reconcile
    reconcile.run(CFG)

    mock_lark.update_record.assert_not_called()


# ---- Change D: two-tier dispatcher (full daily, incremental in between) ----

import time as _time


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_full_sweep_when_no_modified_time_field(mock_lark, mock_jira, mget, mset):
    """No modified-time field on the table → must do a full sweep."""
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = None  # field absent
    now = int(_time.time() * 1000)
    mget.side_effect = lambda k: str(now - 3600_000)  # recent timestamps
    mock_lark.fetch_all_records.return_value = []
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    mock_lark.fetch_all_records.assert_called_once()  # full path
    mock_lark.search_records_modified_since.assert_not_called()


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_full_sweep_when_no_prior_timestamp(mock_lark, mock_jira, mget, mset):
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = "Last modified time"
    mget.side_effect = lambda k: None  # never reconciled before
    mock_lark.fetch_all_records.return_value = []
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    mock_lark.fetch_all_records.assert_called_once()
    mock_lark.search_records_modified_since.assert_not_called()


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_full_sweep_when_last_full_is_stale(mock_lark, mock_jira, mget, mset):
    """Last full sweep > 24 h ago → force a full sweep even if a recent
    incremental timestamp exists."""
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = "Last modified time"
    now = int(_time.time() * 1000)
    def _g(k):
        if k == "last_full_reconcile_ts":
            return str(now - 25 * 3600_000)   # 25 h ago — stale
        return str(now - 3600_000)            # last_any recent
    mget.side_effect = _g
    mock_lark.fetch_all_records.return_value = []
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    mock_lark.fetch_all_records.assert_called_once()
    mock_lark.search_records_modified_since.assert_not_called()


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_incremental_used_when_recent(mock_lark, mock_jira, mget, mset):
    """mod field + recent full + recent any → incremental path, no full fetch."""
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = "Last modified time"
    now = int(_time.time() * 1000)
    mget.side_effect = lambda k: str(now - 3600_000)  # 1 h ago for both
    mock_lark.search_records_modified_since.return_value = []
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    mock_lark.search_records_modified_since.assert_called_once()
    mock_lark.fetch_all_records.assert_not_called()
    # Jira fetched incrementally with an updated_since JQL bound.
    _, kwargs = mock_jira.fetch_all_issues.call_args
    assert kwargs.get("updated_since")
    # last_reconcile_ts advanced; last_full_reconcile_ts NOT (no full sweep).
    keys_set = [c.args[0] for c in mset.call_args_list]
    assert "last_reconcile_ts" in keys_set
    assert "last_full_reconcile_ts" not in keys_set


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_incremental_does_not_delete_orphans(mock_lark, mock_jira, mget, mset):
    """A Jira issue gone from the changed set must NOT be deleted on the
    incremental path — orphan cleanup is deferred to the daily full sweep."""
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = "Last modified time"
    now = int(_time.time() * 1000)
    mget.side_effect = lambda k: str(now - 3600_000)
    # A stale Lark record shows up in the modified set but its Jira issue
    # isn't in the (empty) changed-issues result.
    mock_lark.search_records_modified_since.return_value = [LARK_RECORD]
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    mock_lark.delete_record.assert_not_called()


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_incremental_falls_back_to_full_on_error(mock_lark, mock_jira, mget, mset):
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = "Last modified time"
    now = int(_time.time() * 1000)
    mget.side_effect = lambda k: str(now - 3600_000)
    mock_lark.search_records_modified_since.side_effect = RuntimeError("boom")
    mock_lark.fetch_all_records.return_value = []
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    mock_lark.fetch_all_records.assert_called_once()  # fell back to full


@patch("reconcile._set_setting")
@patch("reconcile._get_setting")
@patch("reconcile.jira_api")
@patch("reconcile.lark_api")
def test_incremental_buffer_applied(mock_lark, mock_jira, mget, mset):
    """search since_ms must be last_reconcile_ts minus the 10-min buffer."""
    mock_lark.get_token.return_value = "tok"
    mock_lark.find_modified_time_field.return_value = "Last modified time"
    now = int(_time.time() * 1000)
    last_any = now - 3600_000
    def _g(k):
        return str(last_any) if k == "last_reconcile_ts" else str(now - 3600_000)
    mget.side_effect = _g
    mock_lark.search_records_modified_since.return_value = []
    mock_jira.fetch_all_issues.return_value = []

    import reconcile
    reconcile.run(CFG)

    since_arg = mock_lark.search_records_modified_since.call_args[0][4]
    assert since_arg == max(0, last_any - 10 * 60 * 1000)
