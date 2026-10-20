import os
import threading
import time

import pytest

import config
import field_mappings
import history
import projects


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")
    monkeypatch.setenv("LARK_BASE_TOKEN", "BASE1")
    monkeypatch.setenv("LARK_TABLE_ID", "tblEnv")
    for k in ("JIRA_EMAIL", "JIRA_TOKEN", "JIRA_DOMAIN", "LARK_APP_ID", "LARK_APP_SECRET"):
        monkeypatch.setenv(k, "x")
    projects._projects = {}
    projects._persisted = False
    yield
    projects._projects = {}
    projects._persisted = False


@pytest.fixture
def no_supabase(monkeypatch):
    monkeypatch.setattr(history, "_get_client", lambda: None)


def _two_projects(no_supabase_fixture=None):
    projects.load_all()
    projects.create({"jira_project_key": "TWO", "lark_base_token": "BASE1", "lark_table_id": "t2"})


def test_fallback_project_without_supabase(no_supabase):
    projects.load_all()
    p = projects.current()
    assert p.id == 1 and p.jira_project_key == os.environ["JIRA_PROJECT"]
    assert p.assignee_map == config.JIRA_TO_LARK_ASSIGNEE
    assert set(p.allowed_jira_types) == set(config.DEFAULT_SYNC_TYPES)
    assert p.sync_enabled and p.reconcile_enabled and p.setup_state == "active"


def test_get_cfg_uses_current_project(no_supabase):
    projects.load_all()
    cfg = config.get_cfg()
    assert cfg["JIRA_PROJECT"] == "PROJ" and cfg["LARK_TABLE_ID"] == "tblEnv"
    assert cfg["PROJECT_ID"] == 1 and cfg["JIRA_BOARD_ID"] is None
    projects.create({"jira_project_key": "TWO", "lark_base_token": "B2", "lark_table_id": "t2"})
    with projects.use(2):
        assert config.get_cfg()["JIRA_PROJECT"] == "TWO"


def test_set_active_table_updates_current_project(no_supabase):
    projects.load_all()
    config.set_active_table("tblNew", "New")
    assert projects.current().lark_table_id == "tblNew"
    assert config.get_cfg()["LARK_TABLE_ID"] == "tblNew"


def test_set_active_table_persists(fake_supabase):
    projects.bootstrap_default()
    config.set_active_table("tblNew", "New")
    row = fake_supabase.rows("sync_projects")[0]
    assert (row["lark_table_id"], row["lark_table_name"]) == ("tblNew", "New")


def test_use_switches_and_restores(no_supabase):
    _two_projects()
    with projects.use(2):
        assert projects.current_id() == 2
    assert projects.current_id() == 1


def test_use_is_isolated_per_thread(no_supabase):
    _two_projects()
    inside, seen = threading.Event(), {}

    def worker():
        with projects.use(2):
            inside.set()
            time.sleep(0.2)
            seen["a"] = projects.current_id()

    t = threading.Thread(target=worker)
    t.start()
    inside.wait()
    seen["main"] = projects.current_id()
    t.join()
    assert seen == {"a": 2, "main": 1}


def test_find_by_lark_needs_table_match(no_supabase):
    projects.load_all()
    projects.update(1, {"lark_base_token": "B", "lark_table_id": "t1"})
    projects.create({"jira_project_key": "TWO", "lark_base_token": "B", "lark_table_id": "t2"})
    assert projects.find_by_lark("B", "t2").id == 2
    assert projects.find_by_lark("B", "t1").id == 1
    assert projects.find_by_lark("B", "t9") is None
    assert projects.find_by_jira_key("TWO").id == 2


def test_create_defaults_to_draft_and_off(no_supabase):
    projects.load_all()
    p = projects.create({"jira_project_key": "TWO", "lark_base_token": "B2", "lark_table_id": "t2",
                         "sync_enabled": True, "reconcile_enabled": True, "setup_state": "active"})
    assert (p.sync_enabled, p.reconcile_enabled, p.setup_state) == (False, False, "draft")
    assert p.id == 2


def test_create_rejects_duplicate_jira_key(no_supabase):
    projects.load_all()
    with pytest.raises(ValueError):
        projects.create({"jira_project_key": "PROJ", "lark_base_token": "B2", "lark_table_id": "t2"})


def test_create_rejects_duplicate_base_and_table(no_supabase):
    projects.load_all()
    with pytest.raises(ValueError):
        projects.create({"jira_project_key": "TWO", "lark_base_token": "BASE1", "lark_table_id": "tblEnv"})


def test_create_persists_with_supabase(fake_supabase):
    projects.bootstrap_default()
    p = projects.create({"jira_project_key": "TWO", "lark_base_token": "B2", "lark_table_id": "t2"})
    assert p.id == 2 and fake_supabase.count("sync_projects") == 2
    assert projects.get(2).setup_state == "draft"


def test_bootstrap_idempotent(fake_supabase):
    p1 = projects.bootstrap_default()
    p2 = projects.bootstrap_default()
    assert p1.id == p2.id and fake_supabase.count("sync_projects") == 1


def test_bootstrap_seeds_from_live_settings(fake_supabase):
    fake_supabase.set_setting("active_table_id", "tblX")
    fake_supabase.set_setting("active_table_name", "Tbl X")
    fake_supabase.set_setting("sync_enabled", "false")
    fake_supabase.set_setting("allowed_jira_types", '["Epic"]')
    p = projects.bootstrap_default()
    assert (p.lark_table_id, p.sync_enabled, p.allowed_jira_types) == ("tblX", False, ["Epic"])
    assert p.lark_table_name == "Tbl X" and p.reconcile_enabled is True
    assert p.jira_project_key == "PROJ" and p.setup_state == "active"


def test_bootstrap_backfills_project_id_and_missing_system_rows(fake_supabase):
    for d in field_mappings._DEFAULTS:
        if d["jira_field"] == "status":
            continue
        row = {k: v for k, v in d.items() if k != "id"}
        fake_supabase.insert_row("field_mappings", row)
    fake_supabase.insert_row("field_mappings", {
        "lark_field": "Custom", "jira_field": "customfield_999", "is_system": False})
    projects.bootstrap_default()
    rows = fake_supabase.rows("field_mappings")
    assert all(r["project_id"] == 1 for r in rows)
    assert {r["jira_field"] for r in rows if r["is_system"]} == \
        {m["jira_field"] for m in field_mappings._DEFAULTS}
    assert len([r for r in rows if r["jira_field"] == "customfield_999"]) == 1


def test_bootstrap_copies_reconcile_watermarks(fake_supabase):
    fake_supabase.set_setting("last_reconcile_ts", "123")
    fake_supabase.set_setting("last_full_reconcile_ts", "99")
    projects.bootstrap_default()
    kv = {r["key"]: r["value"] for r in fake_supabase.rows("settings")}
    assert kv["last_reconcile_ts:1"] == "123" and kv["last_full_reconcile_ts:1"] == "99"
    assert kv["last_reconcile_ts"] == "123"  # global left untouched


def test_bootstrap_without_supabase_returns_env_project(no_supabase):
    assert projects.bootstrap_default().id == 1
