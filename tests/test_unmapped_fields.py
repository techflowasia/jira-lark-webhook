"""The mapping table decides what syncs: a system field with no active
mapping row is never read or written on either side, and the required
mappings (Title, Jira Key, Type) cannot be removed."""
from unittest.mock import MagicMock, patch

import pytest

import config, dedup, field_mappings, history, index, jira_handler
import lark_api, lark_handler, projects, reconcile
from tests.conftest import P2_BASE, P2_TABLE
import utils
from utils import _jira_date_to_lark_ts, _jira_datetime_to_lark_ts

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}
CFG2 = {**CFG, "JIRA_PROJECT": "TWO", "LARK_BASE_TOKEN": P2_BASE,
        "LARK_TABLE_ID": P2_TABLE, "PROJECT_ID": 2}

FOUR = {"Title", "Jira Key", "Type", "Assignee"}


def _row(lark, jira, direction="both", system=True, active=True, **kw):
    return {"id": kw.pop("id", 0), "lark_field": lark, "jira_field": jira,
            "jira_label": lark, "direction": direction, "field_type": "text",
            "is_system": system, "active": active, **kw}


FOUR_ROWS = [
    _row("Title", "summary"),
    _row("Jira Key", "key", "jira_to_lark"),
    _row("Type", "issuetype"),
    _row("Assignee", "assignee"),
]


def _full_issue(key, itype="Epic", assignee="Ada Lovelace", parent=None):
    return {"key": key, "fields": {
        "summary": f"S {key}", "issuetype": {"name": itype},
        "assignee": {"displayName": assignee},
        config.JF_STORY_POINTS: 5.0,
        config.JF_ACTUAL_START: "2026-01-02T10:00:00.000+0700",
        config.JF_ACTUAL_END: "2026-01-09T10:00:00.000+0700",
        config.JF_START: "2026-01-01", "duedate": "2026-01-31",
        "status": {"name": "In Progress"},
        "parent": {"key": parent} if parent else None,
        config.JF_SPRINT: [{"id": 7, "name": "Sprint 1"}],
        "fixVersions": [{"id": "100", "name": "v1"}],
    }}


@pytest.fixture(autouse=True)
def _clean():
    dedup._cache.clear()
    history._fallback.clear()
    yield
    dedup._cache.clear()
    for pid in (1, 2):
        with projects.use(pid):
            index._jira_to_lark.clear()
            index._lark_to_jira.clear()


@pytest.fixture
def four(two_projects):
    """Project 2 maps only Title, Jira Key, Type, Assignee."""
    p1, p2 = two_projects
    field_mappings._by_project[p2.id] = [dict(r) for r in FOUR_ROWS]
    with projects.use(p2.id):
        index.add("TWO-1", "recEpic2")   # a linked parent
    return p2


@pytest.fixture
def mock_lark():
    with patch("jira_handler.lark_api") as m:
        m.get_token.return_value = "tok"
        m.create_record.return_value = "recNew"
        yield m


# ── field_mappings primitives ────────────────────────────────────────────

def test_unmapped_system_field_is_none(four):
    with projects.use(2):
        assert field_mappings.F_TITLE == "Title"
        assert field_mappings.F_END is None
        assert field_mappings.F_PARENT is None
        assert field_mappings.F_MD is None


def test_inactive_system_row_is_unmapped(two_projects):
    field_mappings._by_project[2] = [dict(r) for r in FOUR_ROWS] + [
        _row("Timeline - End", "duedate", active=False)]
    with projects.use(2):
        assert field_mappings.F_END is None


def test_syncs():
    field_mappings._cache = [_row("A", "a", "both"), _row("B", "b", "jira_to_lark"),
                             _row("C", "c", "lark_to_jira"),
                             _row("D", "d", "both", active=False)]
    try:
        assert field_mappings.syncs("A", "lark_to_jira") and field_mappings.syncs("A", "jira_to_lark")
        assert field_mappings.syncs("B", "jira_to_lark") and not field_mappings.syncs("B", "lark_to_jira")
        assert field_mappings.syncs("C", "lark_to_jira") and not field_mappings.syncs("C", "jira_to_lark")
        assert not field_mappings.syncs("D", "lark_to_jira")
        assert not field_mappings.syncs(None, "lark_to_jira")
        assert not field_mappings.syncs("Nope", "both")
    finally:
        field_mappings._by_project.pop(projects.current_id(), None)


def test_put():
    d = {}
    utils.put(d, None, 1)
    utils.put(d, "X", None)
    assert d == {"X": None}


def test_unloaded_project1_uses_defaults_without_supabase(monkeypatch):
    monkeypatch.setattr(history, "_get_client", lambda: None)
    field_mappings._by_project.pop(projects.current_id(), None)
    assert field_mappings.F_START == "Timeline - Start"
    assert field_mappings.F_PARENT == "Parent items"


def test_project_without_rows_gets_no_defaults(fake_supabase, two_projects):
    fake_supabase.insert_row("field_mappings", dict(_row("Title", "summary"), project_id=1))
    field_mappings.load()
    with projects.use(2):
        assert field_mappings.get_all() == []
        assert field_mappings.F_TITLE is None
    with projects.use(1):
        assert field_mappings.F_TITLE == "Title"


def test_failed_reload_keeps_cached_rows(fake_supabase, two_projects, monkeypatch):
    field_mappings._by_project[2] = [dict(r, lark_field="Name") if r["jira_field"] == "summary"
                                     else dict(r) for r in FOUR_ROWS]

    def boom(name):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(fake_supabase, "table", boom)
    field_mappings.load(2)
    with projects.use(2):
        assert field_mappings.F_TITLE == "Name"
        assert field_mappings.F_END is None


# ── required mappings ───────────────────────────────────────────────────

def test_required_mapping_cannot_be_deleted(fake_supabase):
    key_row = fake_supabase.insert_row("field_mappings", dict(_row("Jira Key", "key"), id=None, project_id=1))
    md_row = fake_supabase.insert_row("field_mappings", dict(_row("R. MD", "customfield_10016"), id=None, project_id=1))
    with pytest.raises(ValueError, match="required"):
        field_mappings.delete(key_row["id"])
    with pytest.raises(ValueError, match="required"):
        field_mappings.upsert({"id": key_row["id"], "active": False})
    with pytest.raises(ValueError, match="required"):
        field_mappings.upsert({"id": key_row["id"], "jira_field": "customfield_1"})
    assert fake_supabase.rows("field_mappings")[0]["active"] is True
    # Renaming the Lark column of a required row is fine; so is deleting a
    # non-required row.
    field_mappings.upsert({"id": key_row["id"], "lark_field": "Key"})
    field_mappings.delete(md_row["id"])
    assert [r["lark_field"] for r in fake_supabase.rows("field_mappings")] == ["Key"]


def test_missing_required_mapping_skips_project(two_projects, mock_lark):
    field_mappings._by_project[2] = [r for r in FOUR_ROWS if r["jira_field"] != "key"]
    jira_handler.process("jira:issue_created", _full_issue("TWO-9"), {}, CFG2)
    mock_lark.create_record.assert_not_called()


# ── Jira → Lark ─────────────────────────────────────────────────────────

def test_jira_create_writes_only_mapped(four, mock_lark):
    jira_handler.process("jira:issue_created", _full_issue("TWO-5", parent="TWO-1"), {}, CFG2)
    fields = mock_lark.create_record.call_args[0][3]
    assert set(fields) == FOUR
    assert fields["Assignee"] == ["Ada"]


def test_jira_update_ignores_unmapped_changelog(four, mock_lark):
    with projects.use(2):
        index.add("TWO-5", "rec5")
    mock_lark.get_cached_or_fetch_record.return_value = {"fields": {
        "Title": "S TWO-5", "Assignee": [{"text": "Ada"}]}}
    changelog = {"items": [
        {"field": "duedate", "fieldId": "duedate", "to": "2026-02-01", "toString": "2026-02-01"},
        {"field": "Story Points", "fieldId": config.JF_STORY_POINTS, "to": None, "toString": "8"},
    ]}
    jira_handler.process("jira:issue_updated", _full_issue("TWO-5", parent="TWO-1"),
                         changelog, CFG2)
    mock_lark.update_record.assert_not_called()


def test_project1_unchanged_with_full_rows(mock_lark):
    field_mappings._cache = [dict(r) for r in field_mappings._DEFAULTS]
    try:
        index.add("PROJ-10", "recParent")
        issue = _full_issue("PROJ-11", "Story", "Tawan Vongsombun", parent="PROJ-10")
        jira_handler.process("jira:issue_created", issue, {}, CFG)
        fields = mock_lark.create_record.call_args[0][3]
        jf = issue["fields"]
        assert fields == {
            "Title": "S PROJ-11",
            "Jira Key": "PROJ-11",
            "Jira URL": "https://test.atlassian.net/browse/PROJ-11",
            "Type": "Story",
            "Assignee": ["Tawan"],
            "R. MD": 5,
            "Jira status": "In Progress",
            "Actual start date": _jira_datetime_to_lark_ts(jf[config.JF_ACTUAL_START]),
            "Actual end date": _jira_datetime_to_lark_ts(jf[config.JF_ACTUAL_END]),
            "Timeline - Start": _jira_date_to_lark_ts("2026-01-01"),
            "Timeline - End": _jira_date_to_lark_ts("2026-01-31"),
            "Parent items": ["recParent"],
            "Release": ["Sprint 1"],
        }
    finally:
        field_mappings._by_project.pop(projects.current_id(), None)


# ── Lark → Jira ─────────────────────────────────────────────────────────

# A Lark record that HAS the default-named columns with values, but project 2
# does not map them — they must never be read.
def _p2_record(rid, jira_key=None):
    return {"record_id": rid, "fields": {
        "Title": "Same", "Type": "Epic", "Jira Key": jira_key,
        "Assignee": [{"text": "Ada"}],
        "Timeline - Start": _jira_date_to_lark_ts("2026-03-01"),
        "Release": "v2", "Parent items": [{"record_ids": ["recEpic2"]}],
        "Jira status": "Done",
    }}


@pytest.fixture
def lark_mocks():
    with patch("lark_handler.lark_api") as ml, patch("lark_handler.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_base_tz_hours.return_value = 7
        mj.get_account_ids.return_value = {"Ada Lovelace": "acc-ada"}
        mj.get_project_versions.return_value = [{"id": "200", "name": "v2"}]
        mj.get_board_id.return_value = "b1"
        mj.get_board_sprints.return_value = [{"id": 9, "name": "v2"}]
        mj.create_issue.return_value = "TWO-77"
        yield ml, mj


def test_lark_update_never_clears_unmapped_jira_field(four, lark_mocks):
    ml, mj = lark_mocks
    with projects.use(2):
        index.add("TWO-5", "rec5")
    ml.get_record.return_value = _p2_record("rec5", "TWO-5")   # no "Timeline - End" column
    mj.get_issue.return_value = {"fields": {
        "summary": "Old", "duedate": "2026-01-31", config.JF_START: "2026-01-01",
        "assignee": {"accountId": "acc-ada"}, "fixVersions": [],
        "parent": None, "status": {"name": "To Do"}, config.JF_SPRINT: []}}
    lark_handler.process({"action": "record_edited", "record_id": "rec5"}, P2_TABLE, CFG2)
    mj.update_issue.assert_called_once()
    assert mj.update_issue.call_args[0][2] == {"summary": "Same"}
    mj.move_to_sprint.assert_not_called()
    mj.transition_issue.assert_not_called()


def test_lark_create_reads_only_mapped(four, lark_mocks):
    ml, mj = lark_mocks
    ml.get_record.return_value = _p2_record("recNew")
    lark_handler.process({"action": "record_added", "record_id": "recNew"}, P2_TABLE, CFG2)
    args, kw = mj.create_issue.call_args
    assert args[1:3] == ("Epic", "Same")
    assert kw == {"start_date": None, "due_date": None,
                  "assignee_id": "acc-ada", "parent_key": None}
    assert ml.update_record.call_args[0][4] == {"Jira Key": "TWO-77"}


def test_lark_status_push_needs_active_row(lark_mocks):
    ml, mj = lark_mocks
    field_mappings._cache = [dict(r) for r in field_mappings._DEFAULTS
                             if r["jira_field"] != "status"] + [
        _row("Jira status", "status", "both", active=False)]
    try:
        index.add("PROJ-5", "rec5")
        ml.get_record.return_value = {"record_id": "rec5", "fields": {
            "Title": "T", "Type": "Epic", "Jira Key": "PROJ-5", "Jira status": "Done"}}
        mj.get_issue.return_value = {"fields": {"summary": "T", "status": {"name": "To Do"}}}
        lark_handler.process({"action": "record_edited", "record_id": "rec5"}, "tbl", CFG)
        mj.transition_issue.assert_not_called()
    finally:
        field_mappings._by_project.pop(projects.current_id(), None)


def test_relevant_lark_fields_excludes_unmapped(four):
    with projects.use(2):
        assert None not in lark_handler._relevant_lark_fields()
        assert lark_handler._relevant_lark_fields() == {"Title", "Assignee"}


# ── reconcile / backfill ────────────────────────────────────────────────

@pytest.fixture
def rec_mocks():
    with patch("reconcile.lark_api") as ml, patch("reconcile.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_base_tz_hours.return_value = 7
        ml.create_record.side_effect = lambda t, b, tb, f: f"rec-{f.get('Jira Key')}"
        mj.get_account_ids.return_value = {"Ada Lovelace": "acc-ada"}
        mj.create_issue.return_value = "TWO-88"
        yield ml, mj


def test_reconcile_skips_unmapped(four, rec_mocks):
    ml, mj = rec_mocks
    linked = _p2_record("rec5", "TWO-5")
    ml.fetch_all_records.return_value = [linked]
    mj.fetch_all_issues.return_value = [_full_issue("TWO-5", parent="TWO-1"),
                                        _full_issue("TWO-6", parent="TWO-1")]
    reconcile.run(CFG2)
    for call in ml.update_record.call_args_list:
        assert set(call[0][4]) <= FOUR
    ml.create_record.assert_called_once()
    assert set(ml.create_record.call_args[0][3]) == FOUR


def test_backfill_create_skips_unmapped(four, rec_mocks):
    ml, mj = rec_mocks
    linked = _p2_record("rec5", "TWO-5")
    unlinked = _p2_record("recU")
    unlinked["fields"]["Title"] = "Only in Lark"
    ml.fetch_all_records.return_value = [linked, unlinked]
    mj.fetch_all_issues.return_value = [_full_issue("TWO-5", parent="TWO-1"),
                                        _full_issue("TWO-6", parent="TWO-1")]
    out = reconcile.backfill(CFG2)
    assert out["created_lark"] == 1 and out["created_jira"] == 1
    assert set(ml.create_record.call_args[0][3]) == FOUR
    args, kw = mj.create_issue.call_args
    assert kw == {"start_date": None, "due_date": None,
                  "assignee_id": "acc-ada", "parent_key": None}
    for call in ml.update_record.call_args_list:
        assert set(call[0][4]) <= FOUR


# ── lark_api write filter ───────────────────────────────────────────────

def _resp(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


@pytest.fixture
def mock_requests():
    sent = []

    def fake(method, url, **kw):
        if method == "GET" and url.endswith("/fields"):
            return _resp({"code": 0, "data": {"items": [
                {"field_name": "Title", "field_id": "f1", "type": 1},
                {"field_name": "Jira Key", "field_id": "f2", "type": 1}]}})
        sent.append((method, kw.get("json")))
        return _resp({"code": 0, "data": {"record": {"record_id": "recX"}}})

    lark_api.invalidate_fields_cache()
    with patch("lark_api._request", side_effect=fake):
        yield sent
    lark_api.invalidate_fields_cache()


def test_write_drops_unknown_lark_column(mock_requests):
    lark_api.update_record("tok", "bX", "tX", "rec1", {"Title": "x", "R. MD": 3})
    assert mock_requests == [("PUT", {"fields": {"Title": "x"}})]
    rows = [r for r in history._fallback if r["status"] == "skipped"]
    assert rows and rows[0]["description"] == "Lark column 'R. MD' not found — field not synced"
    assert rows[0]["lark_id"] == "rec1"


def test_create_drops_unknown_lark_column(mock_requests):
    assert lark_api.create_record("tok", "bX", "tX",
                                  {"Title": "x", "Jira Key": "K-1", "R. MD": 3}) == "recX"
    assert mock_requests == [("POST", {"fields": {"Title": "x", "Jira Key": "K-1"}})]


def test_create_refuses_without_jira_key_column(mock_requests):
    field_mappings._cache = [_row("Title", "summary"), _row("Key", "key"), _row("Type", "issuetype")]
    try:
        with pytest.raises(RuntimeError, match="Key"):
            lark_api.create_record("tok", "bX", "tX", {"Title": "x", "Key": "K-1"})
        assert mock_requests == []
    finally:
        field_mappings._by_project.pop(projects.current_id(), None)


def test_update_with_only_unknown_columns_sends_nothing(mock_requests):
    lark_api.update_record("tok", "bX", "tX", "rec1", {"R. MD": 3})
    assert mock_requests == []
