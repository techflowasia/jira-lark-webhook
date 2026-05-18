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
    assert dedup.is_ours("lark_delete:recDEL")


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


# ---- Regression: multi-select Release must not become one combined option ----

@patch("jira_handler.lark_api")
def test_sprint_changelog_splits_into_separate_release_options(mock_lark):
    """Jira sprint changelog toString is comma-joined ("A, B"). It must be
    written to Lark as separate multi-select values, not one "A, B" option."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Release": []}}
    changelog = {"items": [{"field": "customfield_10020",
                            "toString": "VR Sprint 2, Beta 1", "to": None}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Release"] == ["VR Sprint 2", "Beta 1"]


@patch("jira_handler.lark_api")
def test_sprint_changelog_skips_when_release_set_matches(mock_lark):
    """No redundant write (loop guard) when Lark Release already holds the
    same set of names, regardless of order."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Release": ["Beta 1", "VR Sprint 2"]}}
    changelog = {"items": [{"field": "customfield_10020",
                            "toString": "VR Sprint 2, Beta 1", "to": None}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_not_called()


# ---- Regression: Bug 2 — Jira Start/End date → Lark (was unrecognized) ----

def test_jira_date_to_lark_ts_round_trips_bangkok():
    """Loop-safety: Jira date → Lark ms → Lark date must be the SAME day,
    and match Lark's native Bangkok-midnight storage (no redundant writes)."""
    from utils import _jira_date_to_lark_ts, _lark_ts_to_jira_date
    ts = _jira_date_to_lark_ts("2026-06-05")
    assert ts is not None
    assert _lark_ts_to_jira_date(ts) == "2026-06-05"


@patch("jira_handler.lark_api")
def test_duedate_changelog_syncs_to_lark_end(mock_lark):
    from utils import _jira_date_to_lark_ts
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {}}
    changelog = {"items": [{"field": "duedate", "fieldId": "duedate",
                            "to": "2026-06-05", "toString": "2026-06-05 00:00:00.0"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Timeline - End"] == _jira_date_to_lark_ts("2026-06-05")


@patch("jira_handler.lark_api")
def test_startdate_changelog_syncs_to_lark_start(mock_lark):
    from utils import _jira_date_to_lark_ts
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {}}
    changelog = {"items": [{"field": "Start date", "fieldId": "customfield_10015",
                            "to": "2026-06-05", "toString": "5/Jun/26"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Timeline - Start"] == _jira_date_to_lark_ts("2026-06-05")


@patch("jira_handler.lark_api")
def test_startdate_no_redundant_write_when_already_matching(mock_lark):
    """Loop guard: if Lark already holds the Bangkok-midnight ts, no write."""
    from utils import _jira_date_to_lark_ts
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    same = _jira_date_to_lark_ts("2026-06-05")
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Timeline - Start": same}}
    changelog = {"items": [{"field": "Start date", "fieldId": "customfield_10015",
                            "to": "2026-06-05", "toString": "5/Jun/26"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_not_called()


# ---- Regression: Bug 1 — parent change fires 'IssueParentAssociation' ----

def _issue_with_parent(parent_key):
    return {"key": "PROJ-1",
            "fields": {"summary": "S", "issuetype": {"name": "Story"},
                       "assignee": None, "customfield_10016": None,
                       "customfield_10175": None, "customfield_10176": None,
                       "status": {"name": "To Do"},
                       "parent": {"key": parent_key}}}


@patch("jira_handler.lark_api")
def test_parent_change_syncs_via_issueparentassociation(mock_lark):
    """Jira fires field 'IssueParentAssociation' (fieldId None) for a parent
    change — must pass the gate and sync the new parent link to Lark."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    index._jira_to_lark["VR-257"] = "recParent"
    index._lark_to_jira["recParent"] = "VR-257"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Parent items": []}}
    changelog = {"items": [{"field": "IssueParentAssociation", "fieldId": None,
                            "from": "13453", "to": "13438",
                            "fromString": "VR-270", "toString": "VR-257"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", _issue_with_parent("VR-257"),
                         changelog, CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["Parent items"] == ["recParent"]


@patch("jira_handler.lark_api")
def test_parent_change_deferred_is_logged_not_silent(mock_lark):
    """If the new parent has no Lark record yet, do NOT silently no-op —
    record a 'skipped' history row so the divergence is visible."""
    import history
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    # VR-999 NOT in index
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"Parent items": []}}
    changelog = {"items": [{"field": "IssueParentAssociation", "fieldId": None,
                            "from": "1", "to": "2",
                            "fromString": "VR-1", "toString": "VR-999"}]}

    with patch.object(history, "record") as mrec:
        import jira_handler
        jira_handler.process("jira:issue_updated", _issue_with_parent("VR-999"),
                             changelog, CFG)

    mock_lark.update_record.assert_not_called()
    assert mrec.called, "expected a deferred-parent history row, got silent no-op"
    kw = mrec.call_args.kwargs
    assert kw.get("status") == "skipped"
    assert "VR-999" in kw.get("description", "")


# ---- Regression: Bug 3 — custom Jira→Lark mapping (QA Man day, Number) ----

_QA_MAP = [{"id": 9, "lark_field": "P. QA md", "jira_field": "customfield_10178",
            "jira_label": "QA Manday", "direction": "both",
            "field_type": "number", "is_system": False, "active": True}]


@patch("jira_handler.field_mappings.get_custom_jira_to_lark", return_value=_QA_MAP)
@patch("jira_handler.lark_api")
def test_custom_only_changelog_passes_gate_and_syncs_as_number(mock_lark, _gm):
    """A changelog with ONLY a custom field must pass the gate (was dropped
    silently) and write a NUMBER, not the raw string (NumberFieldConvFail)."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {}}
    changelog = {"items": [{"field": "QA Man day", "fieldId": "customfield_10178",
                            "to": "5", "toString": "5"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_called_once()
    fields = mock_lark.update_record.call_args[0][4]
    assert fields["P. QA md"] == 5
    assert isinstance(fields["P. QA md"], int)  # number, not "5"


@patch("jira_handler.field_mappings.get_custom_jira_to_lark", return_value=_QA_MAP)
@patch("jira_handler.lark_api")
def test_custom_number_no_redundant_write_when_equal(mock_lark, _gm):
    """Loop guard: Lark already holds the same number → no write."""
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"P. QA md": 5}}
    changelog = {"items": [{"field": "QA Man day", "fieldId": "customfield_10178",
                            "to": "5", "toString": "5"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_lark.update_record.assert_not_called()


@patch("jira_handler.field_mappings.get_custom_jira_to_lark", return_value=_QA_MAP)
@patch("jira_handler.lark_api")
def test_custom_number_float_value(mock_lark, _gm):
    index._jira_to_lark["PROJ-1"] = "rec1"
    index._lark_to_jira["rec1"] = "PROJ-1"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = {"fields": {"P. QA md": 1}}
    changelog = {"items": [{"field": "QA Man day", "fieldId": "customfield_10178",
                            "to": "0.5", "toString": "0.5"}]}

    import jira_handler
    jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    fields = mock_lark.update_record.call_args[0][4]
    assert fields["P. QA md"] == 0.5
