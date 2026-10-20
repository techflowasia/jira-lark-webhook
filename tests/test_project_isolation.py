"""Per-project state: every cache, map and watermark is scoped to the
current project, and handler entry points pick the project from cfg."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import config, dedup, field_mappings, history, index, jira_api, jira_handler
import lark_api, lark_handler, projects, reconcile
from tests.conftest import P2_ASSIGNEES, P2_BASE, P2_TABLE, P2_TYPES

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}
CFG2 = {**CFG, "JIRA_PROJECT": "TWO", "LARK_BASE_TOKEN": P2_BASE,
        "LARK_TABLE_ID": P2_TABLE, "PROJECT_ID": 2}


def _issue(key, itype="Epic"):
    return {"key": key, "fields": {
        "summary": f"S {key}", "issuetype": {"name": itype}, "assignee": None,
        "customfield_10016": None, "customfield_10175": None,
        "customfield_10176": None, "status": {"name": "To Do"}, "parent": None}}


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
def mock_lark():
    with patch("jira_handler.lark_api") as m:
        m.get_token.return_value = "tok"
        m.create_record.side_effect = lambda tok, b, t, f: f"rec-{f['Jira Key']}"
        yield m


def test_index_is_per_project():
    with projects.use(1):
        index.add("VR-1", "recA")
    with projects.use(2):
        assert "VR-1" not in index._jira_to_lark
        assert "recA" not in index._lark_to_jira
        index.add("VR-1", "recB")
    with projects.use(1):
        assert index._jira_to_lark["VR-1"] == "recA"
    assert index.count(1) == 1 and index.count(2) == 1


def test_index_rebuild_and_remove_touch_only_current_project():
    with projects.use(1):
        index.add("VR-1", "recA")
    with projects.use(2):
        index.rebuild([{"record_id": "recZ", "fields": {"Jira Key": "TWO-1"}}])
        index.remove_by_jira("VR-1")
    with projects.use(1):
        assert index._jira_to_lark == {"VR-1": "recA"}
    with projects.use(2):
        assert index._jira_to_lark == {"TWO-1": "recZ"}


def test_field_mappings_per_project(two_projects):
    with projects.use(2):
        assert field_mappings.F_TITLE == "Name"
    with projects.use(1):
        assert field_mappings.F_TITLE == "Title"


def test_assignee_map_per_project(two_projects):
    with projects.use(2):
        assert config.jira_to_lark_assignee() == P2_ASSIGNEES
        assert config.lark_to_jira_assignee() == {"Ada": "Ada Lovelace"}
    with projects.use(1):
        assert config.jira_to_lark_assignee() == config.JIRA_TO_LARK_ASSIGNEE
        assert config.lark_to_jira_assignee() == config.LARK_TO_JIRA_ASSIGNEE


def test_sync_types_per_project(two_projects):
    with projects.use(2):
        assert config.get_allowed_jira_types() == set(P2_TYPES)
        config.save_sync_types(["Bug"], ["Bug", "Task"])
        assert config.get_allowed_jira_types() == {"Bug"}
        assert config.get_allowed_lark_types() == {"Bug", "Task"}
    with projects.use(1):
        assert config.get_allowed_jira_types() == set(config.DEFAULT_SYNC_TYPES)
        assert config.get_allowed_lark_types() == set(config.DEFAULT_SYNC_TYPES)


def test_jira_create_uses_its_projects_mappings(two_projects, mock_lark):
    jira_handler.process("jira:issue_created", _issue("NEW-1"), {}, CFG2)
    fields = mock_lark.create_record.call_args[0][3]
    assert "Name" in fields and "Title" not in fields
    assert mock_lark.create_record.call_args[0][1:3] == (P2_BASE, P2_TABLE)
    with projects.use(2):
        assert index._jira_to_lark["NEW-1"] == "rec-NEW-1"
    with projects.use(1):
        assert "NEW-1" not in index._jira_to_lark


def test_jira_create_skips_types_not_allowed_in_its_project(two_projects, mock_lark):
    # Story is allowed in project 1 but not in project 2.
    jira_handler.process("jira:issue_created", _issue("NEW-2", "Story"), {}, CFG2)
    mock_lark.create_record.assert_not_called()


def test_cfg_without_project_id_uses_current_project(two_projects, mock_lark):
    with projects.use(2):
        jira_handler.process("jira:issue_created", _issue("NEW-3"), {}, CFG2 | {"PROJECT_ID": 2})
        jira_handler.process("jira:issue_created", _issue("NEW-4"), {},
                             {k: v for k, v in CFG2.items() if k != "PROJECT_ID"})
    assert mock_lark.create_record.call_count == 2
    for call in mock_lark.create_record.call_args_list:
        assert "Name" in call[0][3]


def test_concurrent_handlers_do_not_cross(two_projects, mock_lark):
    def slow_token(*a):
        time.sleep(0.002)
        return "tok"
    mock_lark.get_token.side_effect = slow_token

    def run(i):
        if i % 2:
            jira_handler.process("jira:issue_created", _issue(f"NEW-{100 + i}"), {}, CFG2)
        else:
            jira_handler.process("jira:issue_created", _issue(f"PROJ-{100 + i}"), {}, CFG)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(run, range(20)))

    calls = mock_lark.create_record.call_args_list
    assert len(calls) == 20
    for c in calls:
        _, base, table, fields = c[0]
        if fields["Jira Key"].startswith("NEW-"):
            assert (base, table) == (P2_BASE, P2_TABLE)
            assert "Name" in fields and "Title" not in fields
        else:
            assert (base, table) == ("base", "tbl")
            assert "Title" in fields and "Name" not in fields
    assert index.count(2) == 10 and index.count(1) == 10


def test_lark_handler_caches_are_per_project(two_projects):
    with patch("lark_handler.jira_api") as mj:
        mj.get_project_versions.side_effect = [[{"id": "1", "name": "V1"}],
                                               [{"id": "2", "name": "V2"}]]
        with projects.use(1):
            assert lark_handler._get_version_map(CFG) == {"v1": "1"}
        with projects.use(2):
            assert lark_handler._get_version_map(CFG2) == {"v2": "2"}
        with projects.use(1):
            assert lark_handler._get_version_map(CFG) == {"v1": "1"}
        assert mj.get_project_versions.call_count == 2


def test_lark_update_coalescing_is_keyed_by_project(two_projects):
    seen = []
    with patch("lark_handler._handle_update_impl",
               side_effect=lambda *a, **k: seen.append(set(lark_handler._update_in_flight))):
        with projects.use(2):
            lark_handler._handle_update("recX", P2_TABLE, CFG2)
    assert seen == [{(2, "recX")}]
    assert not lark_handler._update_in_flight


def test_lark_process_uses_project_assignees(two_projects):
    with patch("lark_handler.lark_api") as ml, patch("lark_handler.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_base_tz_hours.return_value = 0.0
        ml.get_record.return_value = {"fields": {
            "Name": "From Lark", "Type": "Epic", "Assignee": [{"name": "Ada"}]}}
        mj.get_account_ids.return_value = {"Ada Lovelace": "acc-ada"}
        mj.create_issue.return_value = "TWO-9"
        lark_handler.process({"action": "record_added", "record_id": "recL"}, P2_TABLE, CFG2)
        args, kwargs = mj.create_issue.call_args
        assert args[2] == "From Lark" and kwargs["assignee_id"] == "acc-ada"
        assert ml.get_record.call_args[0][1:3] == (P2_BASE, P2_TABLE)
    with projects.use(2):
        assert index._jira_to_lark["TWO-9"] == "recL"


def test_history_rows_carry_project_id(fake_supabase, two_projects):
    history.record(direction="system", event="config", description="p1")
    with projects.use(2):
        history.record(direction="jira→lark", event="created", jira_key="TWO-1",
                       description="p2", status="error", error="boom")
    rows = fake_supabase.rows("sync_history")
    assert [(r["description"], r["project_id"]) for r in rows] == [("p1", 1), ("p2", 2)]
    assert [r["description"] for r in history.query(project_id=2)["rows"]] == ["p2"]
    assert {r["description"] for r in history.query()["rows"]} == {"p1", "p2"}


def test_history_error_count(fake_supabase):
    now = datetime.now(timezone.utc)
    for pid, status, age in ((1, "error", 1), (2, "error", 1), (2, "error", 48), (2, "ok", 1)):
        fake_supabase.insert_row("sync_history", {
            "project_id": pid, "status": status,
            "ts": (now - timedelta(hours=age)).isoformat()})
    since = now - timedelta(hours=24)
    assert history.error_count(2, since) == 1
    assert history.error_count(1, since) == 1
    assert history.error_count(3, since) == 0


def test_history_error_count_without_supabase(monkeypatch, two_projects):
    monkeypatch.setattr(history, "_get_client", lambda: None)
    with projects.use(2):
        history.record(direction="system", event="x", description="d", status="error")
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert history.error_count(2, since) == 1
    assert [r["description"] for r in history.query(project_id=2)["rows"]] == ["d"]


def test_history_record_retries_without_project_id_column(monkeypatch):
    calls = []

    class Q:
        def insert(self, payload):
            calls.append(dict(payload))
            self.payload = payload
            return self

        def execute(self):
            if "project_id" in self.payload:
                raise Exception("Could not find the 'project_id' column of 'sync_history' in the schema cache")

    client = MagicMock()
    client.table.return_value = Q()
    monkeypatch.setattr(history, "_get_client", lambda: client)
    history.record(direction="system", event="x", description="d")
    assert len(calls) == 2 and "project_id" not in calls[1]


def test_reconcile_watermarks_per_project(fake_supabase, two_projects):
    now_ms = int(time.time() * 1000)
    fake_supabase.set_setting("last_full_reconcile_ts:1", str(now_ms - 1000))
    fake_supabase.set_setting("last_reconcile_ts:1", str(now_ms - 1000))
    with patch("reconcile.lark_api") as ml, \
         patch("reconcile._run_full") as full, patch("reconcile._run_incremental") as inc:
        ml.get_token.return_value = "tok"
        ml.find_modified_time_field.return_value = "fldMod"
        reconcile.run(CFG2)              # no watermark for project 2 → full sweep
        assert full.call_count == 1 and inc.call_count == 0
        reconcile.run(CFG)               # project 1 has fresh watermarks → incremental
        assert inc.call_count == 1 and full.call_count == 1
    kv = {r["key"]: r["value"] for r in fake_supabase.rows("settings")}
    assert "last_full_reconcile_ts:2" in kv and "last_reconcile_ts:2" in kv
    assert kv["last_full_reconcile_ts:1"] == str(now_ms - 1000)
    assert "last_reconcile_ts" not in kv and "last_full_reconcile_ts" not in kv


def test_reconcile_backfill_runs_in_its_project(two_projects):
    seen = []
    with patch("reconcile.lark_api") as ml, patch("reconcile.jira_api") as mj:
        ml.get_token.side_effect = lambda *a: seen.append(projects.current_id()) or "tok"
        ml.fetch_all_records.return_value = [
            {"record_id": "recQ", "fields": {"Jira Key": "TWO-5", "Type": "Epic"}}]
        mj.fetch_all_issues.return_value = []
        ml.get_base_tz_hours.return_value = 0.0
        result = reconcile.backfill(CFG2)
        assert isinstance(result, dict)
        assert mj.fetch_all_issues.call_args[1]["types"] and \
            set(mj.fetch_all_issues.call_args[1]["types"]) == set(P2_TYPES)
    assert seen and seen[0] == 2
    with projects.use(1):
        assert "TWO-5" not in index._jira_to_lark


def test_base_tz_cache_per_base():
    lark_api._base_tz_cache.clear()
    tz = {"baseA": "Asia/Bangkok", "baseB": "UTC"}

    def fake_request(method, url, headers=None):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"code": 0, "data": {"app": {"time_zone": tz[url.rsplit("/", 1)[1]]}}}
        return resp

    with patch("lark_api._request", side_effect=fake_request) as req:
        assert lark_api.get_base_tz_hours("t", "baseA") == 7.0
        assert lark_api.get_base_tz_hours("t", "baseB") == 0.0
        assert lark_api.get_base_tz_hours("t", "baseA") == 7.0
        assert req.call_count == 2
    lark_api.invalidate_base_tz_cache()
    assert not lark_api._base_tz_cache


def test_board_id_from_cfg_skips_lookup():
    with patch("jira_api.requests") as rq:
        assert jira_api.get_board_id({**CFG, "JIRA_BOARD_ID": "42"}) == "42"
        rq.get.assert_not_called()
        rq.get.return_value = MagicMock(ok=True, json=lambda: {"values": [{"id": 7}]})
        assert jira_api.get_board_id({**CFG, "JIRA_BOARD_ID": None}) == "7"
        assert jira_api.get_board_id(CFG) == "7"


def test_field_mappings_load_groups_by_project(fake_supabase, two_projects):
    for pid, name in ((1, "Title"), (2, "Name")):
        fake_supabase.insert_row("field_mappings", {
            "lark_field": name, "jira_field": "summary", "direction": "both",
            "is_system": True, "active": True, "project_id": pid})
    fake_supabase.insert_row("field_mappings", {
        "lark_field": "Legacy", "jira_field": "customfield_1", "direction": "both",
        "is_system": False, "active": True, "project_id": None})
    field_mappings.load()
    with projects.use(1):
        assert [m["lark_field"] for m in field_mappings.get_all()] == ["Title", "Legacy"]
    with projects.use(2):
        assert [m["lark_field"] for m in field_mappings.get_all()] == ["Name"]
        assert field_mappings.F_TITLE == "Name"


def test_field_mappings_load_one_project_leaves_others(fake_supabase, two_projects):
    with projects.use(1):
        field_mappings._cache = [{"lark_field": "Keep", "jira_field": "summary",
                                  "is_system": True}]
    fake_supabase.insert_row("field_mappings", {
        "lark_field": "Fresh", "jira_field": "summary", "is_system": True, "project_id": 2})
    field_mappings.load(2)
    with projects.use(2):
        assert field_mappings.F_TITLE == "Fresh"
    with projects.use(1):
        assert field_mappings.F_TITLE == "Keep"


def test_field_mappings_upsert_and_delete_are_scoped(fake_supabase, two_projects):
    p1_row = fake_supabase.insert_row("field_mappings", {
        "lark_field": "Title", "jira_field": "summary", "is_system": True, "project_id": 1})
    with projects.use(2):
        saved = field_mappings.upsert({"lark_field": "QA", "jira_field": "cf_9",
                                       "direction": "both", "is_system": False, "active": True})
        assert saved["project_id"] == 2
        # Project 2 cannot edit or delete project 1's row by id.
        field_mappings.upsert({"id": p1_row["id"], "lark_field": "Hacked"})
        field_mappings.delete(p1_row["id"])
        field_mappings.update_lark_field(p1_row["id"], "Hacked")
    rows = {r["id"]: r for r in fake_supabase.rows("field_mappings")}
    assert rows[p1_row["id"]]["lark_field"] == "Title"
    field_mappings.load(1)
    with projects.use(1):
        assert [m["lark_field"] for m in field_mappings.get_all()] == ["Title"]


def test_field_mappings_copy_from(fake_supabase, two_projects):
    for name in ("Title", "Assignee"):
        fake_supabase.insert_row("field_mappings", {
            "lark_field": name, "jira_field": name.lower(), "is_system": True, "project_id": 1})
    p3 = projects.create({"jira_project_key": "THREE", "lark_base_token": "b3",
                          "lark_table_id": "t3"})
    assert field_mappings.copy_from(1, p3.id) == 2
    rows = fake_supabase.rows("field_mappings")
    assert sorted(r["lark_field"] for r in rows if r["project_id"] == p3.id) == ["Assignee", "Title"]
    assert len([r for r in rows if r["project_id"] == 1]) == 2
    with projects.use(p3.id):
        assert field_mappings.F_TITLE == "Title"
    field_mappings._by_project.pop(p3.id, None)


def test_index_rebuild_uses_projects_jira_key_column(two_projects):
    field_mappings._by_project[2] = [
        dict(d, lark_field="Key") if d["jira_field"] == "key" else dict(d)
        for d in field_mappings._by_project[2]]
    recs = [{"record_id": "recK", "fields": {"Key": "TWO-7", "Jira Key": "WRONG-1"}},
            {"record_id": "recJ", "fields": {"Jira Key": "PROJ-7"}}]
    with projects.use(2):
        index.rebuild(recs)
        assert index._jira_to_lark == {"TWO-7": "recK"}
    with projects.use(1):
        index.rebuild(recs)
        assert index._jira_to_lark == {"WRONG-1": "recK", "PROJ-7": "recJ"}


def test_bootstrap_adopts_null_project_history_rows(fake_supabase):
    saved = (projects._projects, projects._persisted)
    try:
        fake_supabase.insert_row("sync_history", {"description": "old", "status": "error",
                                                  "project_id": None})
        fake_supabase.insert_row("sync_history", {"description": "other", "project_id": 9})
        p = projects.bootstrap_default()
        rows = {r["description"]: r["project_id"] for r in fake_supabase.rows("sync_history")}
        assert rows["old"] == p.id and rows["other"] == 9
        projects.bootstrap_default()  # idempotent rerun
        rows = {r["description"]: r["project_id"] for r in fake_supabase.rows("sync_history")}
        assert rows["old"] == p.id and rows["other"] == 9
        assert "old" in [r["description"] for r in history.query(project_id=p.id)["rows"]]
    finally:
        projects._projects, projects._persisted = saved
