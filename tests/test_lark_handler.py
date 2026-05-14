"""Tests for lark_handler: create/update/delete + loop prevention."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import patch, MagicMock
import dedup, index

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}

RECORD = {
    "record_id": "rec001",
    "fields": {
        "Title": "My Story",
        "Type": "Story",
        "Jira Key": None,
        "Assignee": None,
        "Timeline - Start": None,
        "Timeline - End": None,
        "Parent items": [{"record_id": "recEpic", "link_record_title": "Epic 1"}],
    }
}


def setup_function():
    dedup._cache.clear()
    index._jira_to_lark.clear()
    index._lark_to_jira.clear()
    index._jira_to_lark["PROJ-10"] = "recEpic"
    index._lark_to_jira["recEpic"] = "PROJ-10"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_create_makes_jira_issue(mock_lark, mock_jira):
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = RECORD
    mock_jira.create_issue.return_value = "PROJ-42"
    mock_jira.get_account_ids.return_value = {}

    import lark_handler
    lark_handler.process({"action": "record_added", "record_id": "rec001"}, "tbl", CFG)

    mock_jira.create_issue.assert_called_once()
    args = mock_jira.create_issue.call_args
    assert args[0][1] == "Story"
    assert args[0][2] == "My Story"

    mock_lark.update_record.assert_called_once()
    written = mock_lark.update_record.call_args[0][4]
    assert written["Jira Key"] == "PROJ-42"
    assert "PROJ-42" in written["Jira URL"]

    assert index._jira_to_lark.get("PROJ-42") == "rec001"
    assert dedup.is_ours("jira:PROJ-42")


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_create_skips_if_dedup_marked(mock_lark, mock_jira):
    dedup.mark("lark:rec001")
    import lark_handler
    lark_handler.process({"action": "record_added", "record_id": "rec001"}, "tbl", CFG)
    mock_jira.create_issue.assert_not_called()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_create_skips_if_already_in_flight(mock_lark, mock_jira):
    """Concurrent record_added events for the same rid: only the first proceeds."""
    import lark_handler
    lark_handler._create_in_flight.add("rec001")
    try:
        lark_handler.process({"action": "record_added", "record_id": "rec001"}, "tbl", CFG)
        mock_jira.create_issue.assert_not_called()
        mock_lark.get_record.assert_not_called()
    finally:
        lark_handler._create_in_flight.discard("rec001")


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_concurrent_creates_only_one_jira_issue(mock_lark, mock_jira):
    """Two threads firing record_added for the same rid in parallel → only one Jira issue."""
    import threading
    import lark_handler

    started = threading.Event()
    proceed = threading.Event()

    def slow_get_record(*a, **kw):
        # First caller blocks here; second caller should already have been bounced.
        started.set()
        proceed.wait(timeout=2)
        return RECORD

    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.side_effect = slow_get_record
    mock_jira.create_issue.return_value = "PROJ-77"
    mock_jira.get_account_ids.return_value = {}

    t1 = threading.Thread(target=lark_handler.process,
                          args=({"action": "record_added", "record_id": "rec001"}, "tbl", CFG))
    t2 = threading.Thread(target=lark_handler.process,
                          args=({"action": "record_added", "record_id": "rec001"}, "tbl", CFG))
    t1.start()
    started.wait(timeout=2)  # t1 is inside the lock now
    t2.start()
    t2.join(timeout=2)  # t2 should bounce off in-flight check immediately
    proceed.set()
    t1.join(timeout=2)

    mock_jira.create_issue.assert_called_once()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_concurrent_updates_coalesce_one_extra_pass(mock_lark, mock_jira):
    """Two threads firing record_edited for the same rid in parallel → second
    coalesces into a single re-run pass on the in-flight handler instead of
    racing on get_record (which would otherwise trip Lark 429s)."""
    import threading
    import lark_handler

    index._lark_to_jira["recU"] = "PROJ-9"
    index._jira_to_lark["PROJ-9"] = "recU"
    rec = {"record_id": "recU",
           "fields": {"Title": "T", "Type": "Story", "Jira Key": "PROJ-9"}}

    started = threading.Event()
    proceed = threading.Event()

    def slow_get_record(*a, **kw):
        if not started.is_set():
            started.set()
            proceed.wait(timeout=2)
        return rec

    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.side_effect = slow_get_record
    mock_jira.get_issue.return_value = {"fields": {"summary": "T"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    t1 = threading.Thread(target=lark_handler.process,
                          args=({"action": "record_edited", "record_id": "recU"}, "tbl", CFG))
    t2 = threading.Thread(target=lark_handler.process,
                          args=({"action": "record_edited", "record_id": "recU"}, "tbl", CFG))
    t1.start()
    started.wait(timeout=2)
    t2.start()
    t2.join(timeout=2)  # bounces off in-flight check, marks pending re-run
    proceed.set()
    t1.join(timeout=2)

    # First pass + one coalesced re-run = exactly 2 get_record calls (not 1 per event,
    # not parallel calls that would race on Lark's QPS cap).
    assert mock_lark.get_record.call_count == 2


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_create_skips_if_jira_key_already_set(mock_lark, mock_jira):
    rec_with_key = {**RECORD, "fields": {**RECORD["fields"], "Jira Key": "PROJ-1"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec_with_key
    import lark_handler
    lark_handler.process({"action": "record_added", "record_id": "rec001"}, "tbl", CFG)
    mock_jira.create_issue.assert_not_called()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_update_pushes_to_jira(mock_lark, mock_jira):
    index._lark_to_jira["rec002"] = "PROJ-5"
    index._jira_to_lark["PROJ-5"] = "rec002"
    rec = {**RECORD, "record_id": "rec002",
           "fields": {**RECORD["fields"], "Jira Key": "PROJ-5",
                      "Title": "Updated title"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "rec002"}, "tbl", CFG)

    mock_jira.update_issue.assert_called_once()
    fields = mock_jira.update_issue.call_args[0][2]
    assert fields["summary"] == "Updated title"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_update_syncs_parent_with_record_ids_shape(mock_lark, mock_jira):
    """Lark's v1 Bitable returns link fields with `record_ids` (plural).
    Regression test for the bug where parent was never written to Jira."""
    index._lark_to_jira["rec010"] = "PROJ-11"
    index._jira_to_lark["PROJ-11"] = "rec010"
    rec = {
        "record_id": "rec010",
        "fields": {
            "Title": "Child story",
            "Type": "Story",
            "Jira Key": "PROJ-11",
            "Parent items": [{
                "record_ids": ["recEpic"],
                "table_id": "tbl",
                "text": "Epic 1",
                "text_arr": ["Epic 1"],
                "type": "text",
            }],
        },
    }
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.get_issue.return_value = {"fields": {"summary": "Child story", "parent": None}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "rec010"}, "tbl", CFG)

    mock_jira.update_issue.assert_called_once()
    fields = mock_jira.update_issue.call_args[0][2]
    assert fields["parent"] == {"key": "PROJ-10"}, f"expected parent PROJ-10, got {fields.get('parent')}"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_update_skips_when_value_matches(mock_lark, mock_jira):
    """Value-comparison loop prevention: if Jira already matches Lark, no write."""
    index._lark_to_jira["rec003"] = "PROJ-7"
    index._jira_to_lark["PROJ-7"] = "rec003"
    rec = {"record_id": "rec003",
           "fields": {"Title": "Same title", "Type": "Story",
                      "Jira Key": "PROJ-7"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.get_issue.return_value = {"fields": {"summary": "Same title"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "rec003"}, "tbl", CFG)
    mock_jira.update_issue.assert_not_called()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_unlinked_record_edited_routes_to_create(mock_lark, mock_jira):
    """record_edited on an unlinked row should create the Jira issue, not silently drop.
    Happens when record_added fired before Type was set, or Lark only emits record_edited."""
    rec = {**RECORD, "record_id": "recNEW"}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.create_issue.return_value = "PROJ-99"
    mock_jira.get_account_ids.return_value = {}

    import lark_handler
    lark_handler.process({"action": "record_edited", "record_id": "recNEW"}, "tbl", CFG)

    mock_jira.create_issue.assert_called_once()
    assert index._jira_to_lark.get("PROJ-99") == "recNEW"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_create_defers_silently_when_type_missing(mock_lark, mock_jira):
    """User created the row but hasn't picked a Type yet — defer without history noise."""
    rec_no_type = {**RECORD, "fields": {**RECORD["fields"], "Type": None}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec_no_type

    import lark_handler, history
    with patch.object(history, "record") as mock_history:
        lark_handler.process({"action": "record_added", "record_id": "rec001"}, "tbl", CFG)
        mock_history.assert_not_called()
    mock_jira.create_issue.assert_not_called()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_delete_proceeds_when_lark_write_dedup_marked(mock_lark, mock_jira):
    """Regression: user deletes a Lark record shortly after our code wrote to it.
    The `lark:{rid}` write-loopback mark must NOT silence the delete — only the
    `lark_delete:{rid}` mark should do that."""
    index._lark_to_jira["recDel"] = "PROJ-30"
    index._jira_to_lark["PROJ-30"] = "recDel"
    # Simulate our recent write to the record (e.g. just wrote Jira Key back)
    dedup.mark("lark:recDel")
    mock_lark.get_record.side_effect = Exception("not found")  # truly gone
    mock_jira.get_issue.return_value = {"fields": {"issuetype": {"name": "Task"}}}

    import lark_handler
    lark_handler.process({"action": "record_deleted", "record_id": "recDel"}, "tbl", CFG)

    mock_jira.delete_issue.assert_called_once_with(CFG, "PROJ-30")


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_delete_skipped_when_delete_dedup_marked(mock_lark, mock_jira):
    """The `lark_delete:{rid}` mark (set when our code deleted the record itself,
    e.g. reconcile or Jira-cascade) must skip the delete handler."""
    index._lark_to_jira["recDel2"] = "PROJ-31"
    index._jira_to_lark["PROJ-31"] = "recDel2"
    dedup.mark("lark_delete:recDel2")

    import lark_handler
    lark_handler.process({"action": "record_deleted", "record_id": "recDel2"}, "tbl", CFG)

    mock_jira.delete_issue.assert_not_called()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_delete_cascades_to_jira(mock_lark, mock_jira):
    """Lark record deletion cascades to delete the linked Jira issue."""
    index._lark_to_jira["rec004"] = "PROJ-9"
    index._jira_to_lark["PROJ-9"] = "rec004"
    # Simulate record truly gone in Lark (get_record raises)
    mock_lark.get_record.side_effect = Exception("not found")
    mock_jira.get_issue.return_value = {"fields": {"issuetype": {"name": "Story"}}}

    import lark_handler
    lark_handler.process({"action": "record_deleted", "record_id": "rec004"}, "tbl", CFG)

    mock_jira.delete_issue.assert_called_once_with(CFG, "PROJ-9")
    assert "PROJ-9" not in index._jira_to_lark
    assert "rec004" not in index._lark_to_jira
    # The cascading delete marks dedup so Jira's webhook firing back is recognized
    assert dedup.is_ours("jira:PROJ-9")


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_delete_failure_keeps_index_link(mock_lark, mock_jira):
    """If the Jira delete fails, we keep the index link so the next reconcile can retry."""
    index._lark_to_jira["rec008"] = "PROJ-14"
    index._jira_to_lark["PROJ-14"] = "rec008"
    mock_lark.get_record.side_effect = Exception("not found")
    mock_jira.get_issue.return_value = {"fields": {"issuetype": {"name": "Task"}}}
    mock_jira.delete_issue.side_effect = Exception("Jira 500")

    import lark_handler
    lark_handler.process({"action": "record_deleted", "record_id": "rec008"}, "tbl", CFG)

    mock_jira.delete_issue.assert_called_once()
    # Index NOT cleared because Jira delete failed
    assert "PROJ-14" in index._jira_to_lark
    assert "rec008" in index._lark_to_jira


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_delete_records_type_from_jira_issuetype(mock_lark, mock_jira):
    """Delete history row should populate Type from Jira's issuetype since
    the Lark record is gone by then."""
    index._lark_to_jira["rec006"] = "PROJ-12"
    index._jira_to_lark["PROJ-12"] = "rec006"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.side_effect = Exception("not found")  # truly gone
    mock_jira.get_issue.return_value = {"fields": {"issuetype": {"name": "Epic"}}}

    import lark_handler, history
    with patch.object(history, "record") as mock_history:
        lark_handler.process({"action": "record_deleted", "record_id": "rec006"}, "tbl", CFG)
        mock_history.assert_called_once()
        kwargs = mock_history.call_args.kwargs
        assert kwargs.get("type") == "Epic"
        assert kwargs.get("event") == "deleted"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_exception_path_records_type_from_lark(mock_lark, mock_jira):
    """When an unexpected error fires during processing, the error row in
    history should still carry the record's Type so the dashboard column
    isn't blank."""
    index._lark_to_jira["rec007"] = "PROJ-13"
    index._jira_to_lark["PROJ-13"] = "rec007"
    rec = {**RECORD, "record_id": "rec007",
           "fields": {**RECORD["fields"], "Jira Key": "PROJ-13",
                      "Type": "Story", "Title": "Some title"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.get_issue.return_value = {"fields": {"summary": "old"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None
    mock_jira.update_issue.side_effect = Exception("boom")  # unhandled

    import lark_handler, history
    with patch.object(history, "record") as mock_history:
        lark_handler.process({"action": "record_edited", "record_id": "rec007"}, "tbl", CFG)
        called_kwargs = [c.kwargs for c in mock_history.call_args_list]
        error_calls = [k for k in called_kwargs if k.get("status") == "error"]
        assert error_calls, "expected an error row to be recorded"
        assert error_calls[0].get("type") == "Story"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_delete_ignores_spurious_event_when_record_still_exists(mock_lark, mock_jira):
    index._lark_to_jira["rec005"] = "PROJ-10"
    index._jira_to_lark["PROJ-10"] = "rec005"
    # Simulate record still alive in Lark (get_record succeeds)
    mock_lark.get_record.return_value = {"record_id": "rec005", "fields": {}}

    import lark_handler
    lark_handler.process({"action": "record_deleted", "record_id": "rec005"}, "tbl", CFG)

    mock_jira.delete_issue.assert_not_called()
    # Index should NOT be unlinked — record is still alive
    assert "PROJ-10" in index._jira_to_lark
    assert "rec005" in index._lark_to_jira
