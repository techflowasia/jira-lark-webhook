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
    import lark_handler
    lark_handler._version_cache.clear()
    lark_handler._version_cache.update({"data": {}, "expires_at": 0})
    lark_handler._sprint_cache.clear()
    lark_handler._sprint_cache.update({"data": {}, "expires_at": 0})


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


# ---- Change B: webhook after_value fast path (skip get_record) ----

_META = {
    "fldTitle":  {"name": "Title",            "type": 1,  "options": {}},
    "fldStart":  {"name": "Timeline - Start",  "type": 5,  "options": {}},
    "fldType":   {"name": "Type",              "type": 3,
                  "options": {"optEpic": "Epic", "optStory": "Story"}},
    "fldAssign": {"name": "Assignee",          "type": 4,
                  "options": {"optNurse": "Nurse", "optMin": "Min"}},
    "fldParent": {"name": "Parent items",      "type": 18, "options": {}},
}


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_update_uses_after_value_when_provided(mock_lark, mock_jira):
    """rid in index + after_value present → get_record is NOT called."""
    index._lark_to_jira["recFP"] = "PROJ-50"
    index._jira_to_lark["PROJ-50"] = "recFP"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_field_meta_by_id.return_value = _META
    mock_jira.get_issue.return_value = {"fields": {"summary": "Old title"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({
        "action": "record_edited", "record_id": "recFP",
        "after_value": [{"field_id": "fldTitle", "field_value": "Brand new title"}],
    }, "tbl", CFG)

    mock_lark.get_record.assert_not_called()
    mock_jira.update_issue.assert_called_once()
    assert mock_jira.update_issue.call_args[0][2]["summary"] == "Brand new title"


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_update_falls_back_to_get_record_when_not_in_index(mock_lark, mock_jira):
    """rid NOT in index → must get_record (auto-discover needs full record)."""
    rec = {"record_id": "recAD",
           "fields": {"Title": "X", "Type": "Story", "Jira Key": "PROJ-60"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_lark.get_field_meta_by_id.return_value = _META
    mock_jira.get_issue.return_value = {"fields": {"summary": "X"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({
        "action": "record_edited", "record_id": "recAD",
        "after_value": [{"field_id": "fldTitle", "field_value": "X"}],
    }, "tbl", CFG)

    mock_lark.get_record.assert_called_once()


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_update_fast_path_falls_back_on_unknown_field(mock_lark, mock_jira):
    """Unknown field_id in payload → safe fallback to get_record."""
    index._lark_to_jira["recUF"] = "PROJ-70"
    index._jira_to_lark["PROJ-70"] = "recUF"
    rec = {"record_id": "recUF", "fields": {"Title": "from get_record"}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_lark.get_field_meta_by_id.return_value = _META
    mock_jira.get_issue.return_value = {"fields": {"summary": "old"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({
        "action": "record_edited", "record_id": "recUF",
        "after_value": [{"field_id": "fldBRANDNEW", "field_value": "x"}],
    }, "tbl", CFG)

    mock_lark.get_record.assert_called_once()


def test_decode_one_covers_every_field_type():
    """Type-level decoding, independent of sync-scope relevance filtering."""
    import lark_handler
    opts = {"optStory": "Story", "optNurse": "Nurse"}
    assert lark_handler._decode_one(1, "hello", {}) == ("hello", True)            # text
    assert lark_handler._decode_one(2, "5", {}) == ("5", True)                    # number
    assert lark_handler._decode_one(3, "optStory", opts) == ("Story", True)       # single-select
    assert lark_handler._decode_one(3, "optGHOST", opts) == (None, False)         # bad option
    assert lark_handler._decode_one(4, '["optNurse"]', opts) == (["Nurse"], True) # multi-select
    assert lark_handler._decode_one(5, "1779037200000", {}) == (1779037200000, True)  # date
    assert lark_handler._decode_one(18, '["rec1"]', {}) == ([{"record_ids": ["rec1"]}], True)
    assert lark_handler._decode_one(1, "", {}) == (None, True)                    # cleared field
    assert lark_handler._decode_one(20, "x", {}) == (None, False)                 # unhandled → fallback


@patch("lark_handler.lark_api")
def test_decode_after_value_date_string(mock_lark):
    mock_lark.get_field_meta_by_id.return_value = _META
    import lark_handler
    out = lark_handler._decode_after_value(
        [{"field_id": "fldStart", "field_value": "1779037200000"}], "tok", CFG)
    assert out == {"Timeline - Start": 1779037200000}
    assert isinstance(out["Timeline - Start"], int)


@patch("lark_handler.lark_api")
def test_decode_after_value_multiselect(mock_lark):
    mock_lark.get_field_meta_by_id.return_value = _META
    import lark_handler
    out = lark_handler._decode_after_value(
        [{"field_id": "fldAssign", "field_value": '["optNurse"]'}], "tok", CFG)
    assert out == {"Assignee": ["Nurse"]}


@patch("lark_handler.lark_api")
def test_decode_after_value_link_field(mock_lark):
    mock_lark.get_field_meta_by_id.return_value = _META
    import lark_handler
    out = lark_handler._decode_after_value(
        [{"field_id": "fldParent", "field_value": '["rec26ZKzNm9ucD"]'}], "tok", CFG)
    assert out == {"Parent items": [{"record_ids": ["rec26ZKzNm9ucD"]}]}


@patch("lark_handler.lark_api")
def test_decode_after_value_unknown_option_forces_fallback(mock_lark):
    """Unknown option on a RELEVANT select field (Assignee) → None (fallback)."""
    mock_lark.get_field_meta_by_id.return_value = _META
    import lark_handler
    out = lark_handler._decode_after_value(
        [{"field_id": "fldAssign", "field_value": '["optGHOST"]'}], "tok", CFG)
    assert out is None  # unknown select option → caller falls back to get_record


@patch("lark_handler.lark_api")
def test_decode_after_value_ignores_irrelevant_fields(mock_lark):
    """A field not in the sync scope must be ignored, not force a fallback."""
    meta = {**_META, "fldFormula": {"name": "Status", "type": 20, "options": {}}}
    mock_lark.get_field_meta_by_id.return_value = meta
    import lark_handler
    out = lark_handler._decode_after_value([
        {"field_id": "fldFormula", "field_value": "{...}"},
        {"field_id": "fldTitle", "field_value": "T"},
    ], "tok", CFG)
    assert out == {"Title": "T"}  # Status ignored, did not return None


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_replayed_after_value_is_idempotent(mock_lark, mock_jira):
    """Same payload twice → second pass writes nothing (value-comparison guard)."""
    index._lark_to_jira["recID"] = "PROJ-80"
    index._jira_to_lark["PROJ-80"] = "recID"
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_field_meta_by_id.return_value = _META
    # After first push, Jira summary matches the new value.
    mock_jira.get_issue.side_effect = [
        {"fields": {"summary": "old"}},
        {"fields": {"summary": "new title"}},
    ]
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    payload = {"action": "record_edited", "record_id": "recID",
               "after_value": [{"field_id": "fldTitle", "field_value": "new title"}]}
    lark_handler.process(dict(payload), "tbl", CFG)
    lark_handler.process(dict(payload), "tbl", CFG)

    mock_jira.update_issue.assert_called_once()  # only the first pass writes


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


# ---- Regression: newly created Jira sprint must sync from Lark Release ----

@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_new_sprint_synced_when_stale_cache_misses(mock_lark, mock_jira):
    """Stale sprint cache misses a just-created sprint → refresh-on-miss must
    still resolve it and call move_to_sprint (the original silent-no-sync bug)."""
    import time as _t
    import lark_handler
    index._lark_to_jira["recSP"] = "PROJ-90"
    index._jira_to_lark["PROJ-90"] = "recSP"

    # Cache is fresh (expires in the future) but does NOT contain the new sprint.
    lark_handler._sprint_cache.update(
        {"data": {"old sprint": 11}, "expires_at": _t.time() + 3600,
         "last_forced": 0})

    rec = {"record_id": "recSP",
           "fields": {"Title": "T", "Jira Key": "PROJ-90",
                      "Release": ["VR Sprint 5"]}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.get_issue.return_value = {"fields": {"summary": "T"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = "board1"
    # Forced refresh returns the freshly created sprint.
    mock_jira.get_board_sprints.return_value = [{"id": 99, "name": "VR Sprint 5"}]

    lark_handler.process({"action": "record_edited", "record_id": "recSP"}, "tbl", CFG)

    mock_jira.move_to_sprint.assert_called_once_with(CFG, 99, "PROJ-90")


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_forced_refresh_throttled_for_version_only_release(mock_lark, mock_jira):
    """A Release name that is never a sprint must not refetch sprints on every
    edit — the forced refresh is throttled to once per interval."""
    import time as _t
    import lark_handler
    index._lark_to_jira["recVO"] = "PROJ-91"
    index._jira_to_lark["PROJ-91"] = "recVO"

    lark_handler._sprint_cache.update(
        {"data": {"some sprint": 5}, "expires_at": _t.time() + 3600,
         "last_forced": _t.time()})  # just force-refreshed → throttled

    rec = {"record_id": "recVO",
           "fields": {"Title": "T", "Jira Key": "PROJ-91",
                      "Release": ["Beta 1 (version only)"]}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_record.return_value = rec
    mock_jira.get_issue.return_value = {"fields": {"summary": "T"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = "board1"
    mock_jira.get_board_sprints.return_value = [{"id": 5, "name": "some sprint"}]

    lark_handler.process({"action": "record_edited", "record_id": "recVO"}, "tbl", CFG)

    # Throttled: no forced sprint refetch, no spurious move_to_sprint.
    mock_jira.get_board_sprints.assert_not_called()
    mock_jira.move_to_sprint.assert_not_called()


# ---- Regression: webhook text fields are JSON-stringified (VR-227 loop) ----

def test_decode_one_text_parses_json_stringified_array():
    """Webhook delivers text as '[{"text":"hi","type":"text"}]' (a STRING).
    Must parse to the list shape so _lark_text yields "hi", not the JSON."""
    import lark_handler
    val, ok = lark_handler._decode_one(
        1, '[{"text":"All chat list / create direct / group chat","type":"text"}]', {})
    assert ok is True
    assert val == [{"text": "All chat list / create direct / group chat",
                    "type": "text"}]
    # _lark_text on the decoded value must give plain text, not JSON.
    from utils import _lark_text
    assert _lark_text(val) == "All chat list / create direct / group chat"


def test_decode_one_text_plain_string_kept():
    import lark_handler
    assert lark_handler._decode_one(1, "plain title", {}) == ("plain title", True)


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_decoder_filters_unchanged_fields(mock_lark, mock_jira):
    """2026-05 reconcile→QA-Manday burst regression.

    Lark sends the full record in BOTH before_value and after_value on every
    record_edited; only the changed fields differ. The decoder must filter to
    only-changed fields, otherwise downstream re-pushes every relevant value
    on every webhook (the spurious 'QA Manday: 1.0' / 'Sprint: Beta 1.2'
    rows when nothing changed)."""
    import lark_handler, field_mappings
    field_mappings._cache = [
        {"id": 99, "lark_field": "P. QA md", "jira_field": "customfield_10178",
         "jira_label": "QA Manday", "direction": "both", "field_type": "number",
         "is_system": False, "active": True},
    ]
    try:
        index._lark_to_jira["recBurst"] = "PROJ-281"
        index._jira_to_lark["PROJ-281"] = "recBurst"

        meta = {
            "fldRel": {"name": "Release", "type": 4,
                       "options": {"optBeta12": "Beta 1.2", "optOld": "Old"}},
            "fldQA":  {"name": "P. QA md", "type": 2, "options": {}},
        }
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = meta
        # Jira already in Beta 1.2 sprint and QA Manday already 1.0
        mock_jira.get_issue.return_value = {"fields": {
            "summary": "T",
            "customfield_10178": 1.0,
            "customfield_10020": [{"id": 5, "name": "Beta 1.2"}],
            "fixVersions": [],
        }}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = "b1"
        mock_jira.get_board_sprints.return_value = [{"id": 5, "name": "Beta 1.2"}]

        # Webhook: only Release option id changed; P. QA md raw value
        # is byte-identical in before and after.
        lark_handler.process({
            "action": "record_edited", "record_id": "recBurst",
            "before_value": [
                {"field_id": "fldRel", "field_value": '["optOld"]'},
                {"field_id": "fldQA",  "field_value": "1.0"},
            ],
            "after_value": [
                {"field_id": "fldRel", "field_value": '["optBeta12"]'},
                {"field_id": "fldQA",  "field_value": "1.0"},
            ],
        }, "tbl", CFG)

        # No issue update should include QA Manday — it didn't change.
        for call in mock_jira.update_issue.call_args_list:
            sent = call[0][2]
            assert "customfield_10178" not in sent, (
                f"QA Manday re-pushed despite unchanged before/after: {sent}")
    finally:
        field_mappings._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_custom_mapping_skips_when_value_matches_jira(mock_lark, mock_jira):
    """Defense-in-depth: even if the decoder doesn't filter (no before_value
    supplied), the custom-mapping loop must value-compare against current
    Jira state and skip re-writing a matching value."""
    import lark_handler, field_mappings
    field_mappings._cache = [
        {"id": 99, "lark_field": "P. QA md", "jira_field": "customfield_10178",
         "jira_label": "QA Manday", "direction": "both", "field_type": "number",
         "is_system": False, "active": True},
    ]
    try:
        index._lark_to_jira["recC"] = "PROJ-100"
        index._jira_to_lark["PROJ-100"] = "recC"

        rec = {"record_id": "recC",
               "fields": {"Title": "T", "Jira Key": "PROJ-100",
                          "P. QA md": 1.0}}
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_record.return_value = rec
        mock_jira.get_issue.return_value = {"fields": {
            "summary": "T", "customfield_10178": 1.0}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None

        # No after_value/before_value → falls back to get_record path; the
        # H1 decoder filter is bypassed, only H2 (this gate) protects us.
        lark_handler.process(
            {"action": "record_edited", "record_id": "recC"}, "tbl", CFG)

        mock_jira.update_issue.assert_not_called()
    finally:
        field_mappings._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_custom_mapping_writes_when_value_differs(mock_lark, mock_jira):
    """Value-compare gate must not over-block: a real change still propagates."""
    import lark_handler, field_mappings
    field_mappings._cache = [
        {"id": 99, "lark_field": "P. QA md", "jira_field": "customfield_10178",
         "jira_label": "QA Manday", "direction": "both", "field_type": "number",
         "is_system": False, "active": True},
    ]
    try:
        index._lark_to_jira["recC2"] = "PROJ-101"
        index._jira_to_lark["PROJ-101"] = "recC2"

        rec = {"record_id": "recC2",
               "fields": {"Title": "T", "Jira Key": "PROJ-101",
                          "P. QA md": 2.5}}
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_record.return_value = rec
        mock_jira.get_issue.return_value = {"fields": {
            "summary": "T", "customfield_10178": 1.0}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None

        lark_handler.process(
            {"action": "record_edited", "record_id": "recC2"}, "tbl", CFG)

        mock_jira.update_issue.assert_called_once()
        sent = mock_jira.update_issue.call_args[0][2]
        assert sent.get("customfield_10178") == 2.5
    finally:
        field_mappings._cache = []


@patch("lark_handler.lark_api")
def test_decode_after_value_no_before_disables_filter(mock_lark):
    """When before_value isn't supplied (legacy / coalesced re-run), the
    decoder must not filter — fall back to including every relevant field."""
    mock_lark.get_field_meta_by_id.return_value = _META
    import lark_handler
    out = lark_handler._decode_after_value(
        [{"field_id": "fldTitle", "field_value": "T"}], "tok", CFG)
    assert out == {"Title": "T"}


@patch("lark_handler.lark_api")
def test_decode_after_value_filters_byte_identical_field(mock_lark):
    """Field present in both before and after with identical raw value → drop."""
    mock_lark.get_field_meta_by_id.return_value = _META
    import lark_handler
    out = lark_handler._decode_after_value(
        after_value=[
            {"field_id": "fldTitle", "field_value": "Same"},
            {"field_id": "fldStart", "field_value": "1779037200000"},
        ],
        token="tok", cfg=CFG,
        before_value=[
            {"field_id": "fldTitle", "field_value": "Same"},
            {"field_id": "fldStart", "field_value": "1779000000000"},
        ],
    )
    # Title unchanged → filtered out. Start changed → kept.
    assert out == {"Timeline - Start": 1779037200000}


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_fast_path_title_writes_plain_text_not_json(mock_lark, mock_jira):
    """End-to-end: a Title edit delivered as a stringified rich-text array
    must push the inner plain text to Jira summary — NOT the JSON wrapper
    (which previously fed the exponential-nesting sync loop)."""
    index._lark_to_jira["recTL"] = "PROJ-227"
    index._jira_to_lark["PROJ-227"] = "recTL"
    meta = {"fldT": {"name": "Title", "type": 1, "options": {}}}
    mock_lark.get_token.return_value = "tok"
    mock_lark.get_field_meta_by_id.return_value = meta
    mock_jira.get_issue.return_value = {"fields": {"summary": "old"}}
    mock_jira.get_account_ids.return_value = {}
    mock_jira.get_project_versions.return_value = []
    mock_jira.get_board_id.return_value = None

    import lark_handler
    lark_handler.process({
        "action": "record_edited", "record_id": "recTL",
        "after_value": [{"field_id": "fldT",
                         "field_value": '[{"text":"All chat list / create direct / group chat","type":"text"}]'}],
    }, "tbl", CFG)

    mock_lark.get_record.assert_not_called()
    mock_jira.update_issue.assert_called_once()
    summary = mock_jira.update_issue.call_args[0][2]["summary"]
    assert summary == "All chat list / create direct / group chat"
    assert "{" not in summary  # no JSON wrapper leaked through


# ---- Jira status (Lark → Jira) sync: VR-256/258 ----
# Bug: when the user sets the 'Jira status' field direction to 'both' on the
# dashboard, status changes in Lark do NOT propagate to Jira — only date
# changes sync. Evidence: webhook log shows only date events for VR-256 while
# the Jira history shows the user toggling status back and forth. Root cause
# was three-fold: (1) the fast-path decoder excluded 'Jira status' from the
# relevant fields set, (2) _handle_update_impl had no status branch at all,
# (3) even if it had, Jira's status can't be set via PUT issue — it requires
# the workflow transitions API.
#
# Fix tested below:
#   - jira_api.transition_issue + get_transitions added
#   - field_mappings.get_direction helper added (returns config direction)
#   - lark_handler._relevant_lark_fields() now includes F_JIRA_STATUS only
#     when direction is 'both' or 'lark_to_jira' (default jira_to_lark stays
#     no-op — don't surprise users who didn't opt in)
#   - lark_handler._handle_update_impl() detects a status change, calls
#     transition_issue, value-compares against current Jira status (loop
#     guard), and skips silently if the workflow has no matching transition

_STATUS_META = {
    "fldStatus": {"name": "Jira status", "type": 3,
                  "options": {"optTodo": "To Do", "optInProg": "In Progress",
                              "optDev": "To do - Dev"}},
    "fldTitle":  {"name": "Title", "type": 1, "options": {}},
}


def _seed_jira_status_map():
    """Default field_mappings cache with 'Jira status' → 'status' direction=jira_to_lark.

    Tests that need a different direction patch `field_mappings._cache[0]['direction']`
    (or replace the list) and call this in a finally to restore the default.
    """
    import field_mappings
    field_mappings._cache = [
        {"id": 12, "lark_field": "Jira status", "jira_field": "status",
         "jira_label": "Status", "direction": "jira_to_lark",
         "field_type": "select", "is_system": True, "active": True},
    ]
    return field_mappings


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_status_sync_direction_both_fires_transition(mock_lark, mock_jira):
    """direction=both: a Lark status change must call transition_issue, not update_issue."""
    fm = _seed_jira_status_map()
    fm._cache[0]["direction"] = "both"
    try:
        index._lark_to_jira["recS1"] = "PROJ-256"
        index._jira_to_lark["PROJ-256"] = "recS1"
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = _STATUS_META
        mock_jira.get_issue.return_value = {"fields": {"status": {"name": "To Do"}}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None
        mock_jira.get_transitions.return_value = [
            {"id": "21", "name": "Start Progress", "to": {"name": "In Progress"}},
            {"id": "31", "name": "Send to Dev",    "to": {"name": "To do - Dev"}},
        ]
        mock_jira.transition_issue.return_value = True

        import lark_handler
        lark_handler.process({
            "action": "record_edited", "record_id": "recS1",
            "after_value": [{"field_id": "fldStatus", "field_value": "optInProg"}],
            "before_value": [{"field_id": "fldStatus", "field_value": "optTodo"}],
        }, "tbl", CFG)

        # Status must go through the transitions API, not update_issue
        mock_jira.transition_issue.assert_called_once_with(CFG, "PROJ-256", "In Progress")
        mock_jira.update_issue.assert_not_called()
    finally:
        fm._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_status_sync_direction_lark_to_jira_fires_transition(mock_lark, mock_jira):
    """direction=lark_to_jira: also pushes to Jira (not just jira_to_lark or both)."""
    fm = _seed_jira_status_map()
    fm._cache[0]["direction"] = "lark_to_jira"
    try:
        index._lark_to_jira["recS2"] = "PROJ-257"
        index._jira_to_lark["PROJ-257"] = "recS2"
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = _STATUS_META
        mock_jira.get_issue.return_value = {"fields": {"status": {"name": "To Do"}}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None
        mock_jira.get_transitions.return_value = [
            {"id": "21", "name": "Start Progress", "to": {"name": "In Progress"}},
        ]
        mock_jira.transition_issue.return_value = True

        import lark_handler
        lark_handler.process({
            "action": "record_edited", "record_id": "recS2",
            "after_value": [{"field_id": "fldStatus", "field_value": "optInProg"}],
            "before_value": [{"field_id": "fldStatus", "field_value": "optTodo"}],
        }, "tbl", CFG)

        mock_jira.transition_issue.assert_called_once_with(CFG, "PROJ-257", "In Progress")
        mock_jira.update_issue.assert_not_called()
    finally:
        fm._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_status_sync_direction_default_skips_lark_to_jira(mock_lark, mock_jira):
    """direction=jira_to_lark (the default): a Lark status change must NOT
    fire a transition. The dashboard hasn't been changed to opt in, so a
    user editing status in Lark should be a local-only edit — pushing to
    Jira would surprise them. This is the safe default that protects
    users who never touched the field mappings table."""
    fm = _seed_jira_status_map()  # direction stays jira_to_lark
    try:
        index._lark_to_jira["recS3"] = "PROJ-258"
        index._jira_to_lark["PROJ-258"] = "recS3"
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = _STATUS_META
        mock_jira.get_issue.return_value = {"fields": {"status": {"name": "To Do"}}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None

        import lark_handler
        lark_handler.process({
            "action": "record_edited", "record_id": "recS3",
            "after_value": [{"field_id": "fldStatus", "field_value": "optInProg"}],
            "before_value": [{"field_id": "fldStatus", "field_value": "optTodo"}],
        }, "tbl", CFG)

        # No transition, no update — the change is local-only.
        mock_jira.transition_issue.assert_not_called()
        mock_jira.update_issue.assert_not_called()
    finally:
        fm._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_status_sync_skipped_when_value_matches_jira(mock_lark, mock_jira):
    """Value-compare loop guard: if the new Lark status equals current Jira
    status, NO Jira call. Without this, every webhook (e.g. a date change
    that also re-fires the status field in the same payload) would push
    a no-op transition and feed a sync loop."""
    fm = _seed_jira_status_map()
    fm._cache[0]["direction"] = "both"
    try:
        index._lark_to_jira["recS4"] = "PROJ-259"
        index._jira_to_lark["PROJ-259"] = "recS4"
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = _STATUS_META
        # Jira already at "In Progress" — Lark change is to the same value
        mock_jira.get_issue.return_value = {"fields": {"status": {"name": "In Progress"}}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None

        import lark_handler
        lark_handler.process({
            "action": "record_edited", "record_id": "recS4",
            "after_value": [{"field_id": "fldStatus", "field_value": "optInProg"}],
            "before_value": [{"field_id": "fldStatus", "field_value": "optInProg"}],
        }, "tbl", CFG)

        mock_jira.transition_issue.assert_not_called()
        mock_jira.update_issue.assert_not_called()
    finally:
        fm._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_status_sync_no_matching_transition_logged_not_raised(mock_lark, mock_jira):
    """Workflow has no transition to the target status (e.g. Done → To Do is
    forbidden by most Jira workflows). transition_issue returns False →
    handler logs a warning and skips. The webhook must NOT 500 — a real
    user change is recorded, just not auto-pushed through a forbidden path."""
    fm = _seed_jira_status_map()
    fm._cache[0]["direction"] = "both"
    try:
        index._lark_to_jira["recS5"] = "PROJ-260"
        index._jira_to_lark["PROJ-260"] = "recS5"
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = _STATUS_META
        mock_jira.get_issue.return_value = {"fields": {"status": {"name": "Done"}}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None
        # Workflow only has forward transitions — no Done → To Do
        mock_jira.get_transitions.return_value = []
        mock_jira.transition_issue.return_value = False

        import lark_handler
        # Must not raise
        lark_handler.process({
            "action": "record_edited", "record_id": "recS5",
            "after_value": [{"field_id": "fldStatus", "field_value": "optTodo"}],
            "before_value": [{"field_id": "fldStatus", "field_value": "optDone"}],
        }, "tbl", CFG)

        mock_jira.transition_issue.assert_called_once()
    finally:
        fm._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_status_sync_transition_failure_does_not_block_other_fields(mock_lark, mock_jira):
    """If the transition raises (5xx, auth, etc.), the rest of the sync
    must not be rolled back — Title / dates written in the same call should
    still land. Tested via the success log: a failed transition just isn't
    added to the 'changed' list (the user sees the failure in the Render
    logs but the rest of the update went through)."""
    fm = _seed_jira_status_map()
    fm._cache[0]["direction"] = "both"
    try:
        index._lark_to_jira["recS6"] = "PROJ-261"
        index._jira_to_lark["PROJ-261"] = "recS6"
        mock_lark.get_token.return_value = "tok"
        mock_lark.get_field_meta_by_id.return_value = _STATUS_META
        mock_jira.get_issue.return_value = {"fields": {
            "summary": "Old", "status": {"name": "To Do"}}}
        mock_jira.get_account_ids.return_value = {}
        mock_jira.get_project_versions.return_value = []
        mock_jira.get_board_id.return_value = None
        mock_jira.get_transitions.return_value = [
            {"id": "21", "to": {"name": "In Progress"}},
        ]
        mock_jira.transition_issue.side_effect = RuntimeError("Jira 500")

        import lark_handler
        # Must not raise
        lark_handler.process({
            "action": "record_edited", "record_id": "recS6",
            "after_value": [
                {"field_id": "fldTitle",  "field_value": "New title"},
                {"field_id": "fldStatus", "field_value": "optInProg"},
            ],
            "before_value": [
                {"field_id": "fldTitle",  "field_value": "Old"},
                {"field_id": "fldStatus", "field_value": "optTodo"},
            ],
        }, "tbl", CFG)

        # Title update still went through
        mock_jira.update_issue.assert_called_once()
        assert mock_jira.update_issue.call_args[0][2]["summary"] == "New title"
        # Transition was attempted and failed gracefully
        mock_jira.transition_issue.assert_called_once()
    finally:
        fm._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_relevant_lark_fields_excludes_status_by_default(mock_lark, mock_jira):
    """_relevant_lark_fields() must NOT include 'Jira status' when the
    configured direction is the default 'jira_to_lark' — including it would
    force a get_record on every Jira-status webhook for users who didn't
    opt in, wasting Lark API quota."""
    import lark_handler, field_mappings
    field_mappings._cache = [
        {"id": 12, "lark_field": "Jira status", "jira_field": "status",
         "jira_label": "Status", "direction": "jira_to_lark",
         "field_type": "select", "is_system": True, "active": True},
    ]
    try:
        assert "Jira status" not in lark_handler._relevant_lark_fields()
    finally:
        field_mappings._cache = []


@patch("lark_handler.jira_api")
@patch("lark_handler.lark_api")
def test_relevant_lark_fields_includes_status_when_both(mock_lark, mock_jira):
    """direction=both: 'Jira status' must be in the relevant set so the
    fast-path decoder picks it up and skips get_record for status-only
    webhooks."""
    import lark_handler, field_mappings
    field_mappings._cache = [
        {"id": 12, "lark_field": "Jira status", "jira_field": "status",
         "jira_label": "Status", "direction": "both",
         "field_type": "select", "is_system": True, "active": True},
    ]
    try:
        assert "Jira status" in lark_handler._relevant_lark_fields()
    finally:
        field_mappings._cache = []


def test_field_mappings_get_direction_returns_configured():
    """field_mappings.get_direction() reads the dashboard-configured direction
    for a field, including system fields (whose direction is user-editable
    even though is_system=True)."""
    import field_mappings
    field_mappings._cache = [
        {"lark_field": "Jira status", "direction": "both"},
        {"lark_field": "Title", "direction": "jira_to_lark"},
    ]
    try:
        assert field_mappings.get_direction("Jira status") == "both"
        assert field_mappings.get_direction("Title") == "jira_to_lark"
        assert field_mappings.get_direction("No Such Field") is None
    finally:
        field_mappings._cache = []


@patch("jira_api.requests")
def test_transition_issue_returns_true_when_transition_found(mock_requests):
    """transitions API returns a transition whose `to.name` matches the
    target — we POST to /transitions with that ID and return True."""
    import jira_api
    get_resp = MagicMock()
    get_resp.json.return_value = {"transitions": [
        {"id": "21", "name": "Start", "to": {"name": "In Progress"}},
        {"id": "31", "name": "Reopen", "to": {"name": "To Do"}},
    ]}
    get_resp.raise_for_status = MagicMock()
    post_resp = MagicMock()
    post_resp.ok = True
    mock_requests.get.return_value = get_resp
    mock_requests.post.return_value = post_resp

    assert jira_api.transition_issue(CFG, "PROJ-1", "In Progress") is True
    mock_requests.get.assert_called_once()
    assert "/rest/api/3/issue/PROJ-1/transitions" in mock_requests.get.call_args[0][0]
    mock_requests.post.assert_called_once()
    assert mock_requests.post.call_args[1]["json"] == {"transition": {"id": "21"}}


@patch("jira_api.requests")
def test_transition_issue_returns_false_when_no_match(mock_requests):
    """No transition in the workflow has `to.name` matching the target
    (e.g. Done → To Do is forbidden). Returns False — the handler logs
    and skips, never forces a path the workflow forbids."""
    import jira_api
    get_resp = MagicMock()
    get_resp.json.return_value = {"transitions": [
        {"id": "11", "name": "Done", "to": {"name": "Done"}},
    ]}
    get_resp.raise_for_status = MagicMock()
    mock_requests.get.return_value = get_resp

    assert jira_api.transition_issue(CFG, "PROJ-1", "To Do") is False
    mock_requests.post.assert_not_called()  # never call POST if no match


@patch("jira_api.requests")
def test_transition_issue_raises_on_http_error(mock_requests):
    """POST /transitions returned 5xx / 401 / 403 → re-raise so the outer
    try/except in process() can log the error to sync_history. Verified by
    checking the POST was called (we don't try to catch the exception class
    because the patch makes `requests.HTTPError` a MagicMock)."""
    import jira_api
    get_resp = MagicMock()
    get_resp.json.return_value = {"transitions": [
        {"id": "21", "to": {"name": "In Progress"}},
    ]}
    get_resp.raise_for_status = MagicMock()
    post_resp = MagicMock()
    post_resp.ok = False
    post_resp.status_code = 500
    post_resp.reason = "Server Error"
    post_resp.text = "internal"
    mock_requests.get.return_value = get_resp
    mock_requests.post.return_value = post_resp

    raised = False
    try:
        jira_api.transition_issue(CFG, "PROJ-1", "In Progress")
    except Exception:
        raised = True
    assert raised, "expected an exception when POST /transitions returns 5xx"
    # Sanity: POST was called (proves the error came from the transitions POST,
    # not the GET).
    mock_requests.post.assert_called_once()
