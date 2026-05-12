"""Tests for jira_handler: create/update/delete + loop prevention."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import patch, MagicMock
import dedup, index

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}

ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "My Epic",
        "issuetype": {"name": "Epic"},
        "assignee": None,
        "customfield_10016": None,
        "customfield_10175": None,
        "customfield_10176": None,
        "status": {"name": "To Do"},
        "parent": None,
    }
}


def setup_function():
    dedup._cache.clear()
    index._jira_to_lark.clear()
    index._lark_to_jira.clear()


@patch("jira_handler.lark_api")
def test_create_makes_lark_record(mock_lark):
    mock_lark.get_token.return_value = "tok"
    mock_lark.create_record.return_value = "recNew"

    import jira_handler
    jira_handler.process("jira:issue_created", ISSUE, {}, CFG)

    mock_lark.create_record.assert_called_once()
    fields = mock_lark.create_record.call_args[0][3]
    assert fields["Title"] == "My Epic"
    assert fields["Jira Key"] == "PROJ-1"
    assert fields["Type"] == "Epic"

    assert index._jira_to_lark.get("PROJ-1") == "recNew"
    assert dedup.is_ours("lark:recNew")


@patch("jira_handler.lark_api")
def test_create_skips_if_dedup_marked(mock_lark):
    dedup.mark("jira:PROJ-2")
    issue = {**ISSUE, "key": "PROJ-2"}
    import jira_handler
    jira_handler.process("jira:issue_created", issue, {}, CFG)
    mock_lark.create_record.assert_not_called()


@patch("jira_handler.lark_api")
def test_create_skips_if_already_linked(mock_lark):
    index._jira_to_lark["PROJ-1"] = "recExisting"
    import jira_handler
    jira_handler.process("jira:issue_created", ISSUE, {}, CFG)
    mock_lark.create_record.assert_not_called()


@patch("jira_handler.lark_api")
def test_update_pushes_summary_to_lark(mock_lark):
    index._jira_to_lark["PROJ-1"] = "recABC"
    index._lark_to_jira["recABC"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Title": "Old title"}}

    changelog = {"items": [{"field": "summary", "toString": "New title", "to": None}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Title"] == "New title"


@patch("jira_handler.lark_api")
def test_update_skips_when_value_matches(mock_lark):
    """Value-comparison loop prevention: if Lark already has the new value, no write."""
    index._jira_to_lark["PROJ-1"] = "recABC"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Title": "X"}}
    changelog = {"items": [{"field": "summary", "toString": "X", "to": None}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    mock_lark.update_record.assert_not_called()


@patch("jira_handler.lark_api")
def test_update_skips_irrelevant_fields(mock_lark):
    index._jira_to_lark["PROJ-1"] = "recABC"
    changelog = {"items": [{"field": "priority", "toString": "High", "to": None}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    mock_lark.update_record.assert_not_called()


@patch("jira_handler.lark_api")
def test_delete_removes_lark_record(mock_lark):
    index._jira_to_lark["PROJ-1"] = "recDEL"
    index._lark_to_jira["recDEL"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"

    import jira_handler
    jira_handler.process("jira:issue_deleted", ISSUE, {}, CFG)

    mock_lark.delete_record.assert_called_once()
    assert "PROJ-1" not in index._jira_to_lark
    assert dedup.is_ours("lark:recDEL")


@patch("jira_handler.lark_api")
def test_story_points_formatted_correctly(mock_lark):
    """Lark number field requires numeric value — strings raise NumberFieldConvFail."""
    index._jira_to_lark["PROJ-1"] = "recSP"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"R. MD": 0}}
    changelog = {"items": [{"field": "customfield_10016", "toString": "5.0", "to": None}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["R. MD"] == 5
    assert isinstance(fields["R. MD"], int)


@patch("jira_handler.lark_api")
def test_story_points_skipped_when_value_matches(mock_lark):
    """Don't write when current Lark value already matches Jira value."""
    index._jira_to_lark["PROJ-1"] = "recSP"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"R. MD": 5}}
    changelog = {"items": [{"field": "customfield_10016", "toString": "5.0", "to": None}]}
    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)
    mock_lark.update_record.assert_not_called()
