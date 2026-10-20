"""Final whole-branch review fixes: C1 (foreign Jira keys), I1 (registry read
failure at boot), I3 (failed mapping load cached forever), I4 (Jira project
key validation) and the minors."""
import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import config, dedup, field_mappings, history, index, jira_api, lark_api
import lark_handler, main, projects, projects_api, reconcile
from tests.conftest import P2_BASE, P2_TABLE

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl", "PROJECT_ID": 1,
}
CFG2 = {**CFG, "JIRA_PROJECT": "TWO", "LARK_BASE_TOKEN": P2_BASE,
        "LARK_TABLE_ID": P2_TABLE, "PROJECT_ID": 2}

client = TestClient(main.app)

# Project #2's Base was duplicated from project #1's with its data: this row
# still carries project #1's Jira key.
FOREIGN = {"record_id": "recF", "fields": {"Jira Key": "PROJ-123", "Name": "Copied",
                                           "Type": "Epic"}}
OWN2 = {"record_id": "recO", "fields": {"Jira Key": "TWO-5", "Name": "Own", "Type": "Epic"}}


@pytest.fixture(autouse=True)
def _clean():
    dedup._cache.clear()
    history._fallback.clear()
    yield
    dedup._cache.clear()
    history._fallback.clear()
    for pid in (1, 2):
        index._maps.pop(pid, None)


def _issue(key, itype="Epic"):
    return {"key": key, "fields": {
        "summary": f"S {key}", "issuetype": {"name": itype}, "assignee": None,
        "customfield_10016": None, "customfield_10175": None,
        "customfield_10176": None, "status": {"name": "To Do"}, "parent": None}}


def _errors():
    return [r for r in history._fallback if r["status"] == "error"]


# ── C1: foreign Jira keys ───────────────────────────────────────────────────

def test_owns_key(two_projects):
    with projects.use(1):
        assert projects.owns_key("PROJ-1") and projects.owns_key("proj-12")
        assert not projects.owns_key("TWO-1") and not projects.owns_key("")
        assert not projects.owns_key("PROJX-1") and not projects.owns_key("PROJ")
    with projects.use(2):
        assert projects.owns_key("TWO-1") and not projects.owns_key("PROJ-123")


def test_index_skips_foreign_keys_and_keeps_own(two_projects):
    with projects.use(2):
        index.rebuild([FOREIGN, OWN2])
        assert index._jira_to_lark == {"TWO-5": "recO"}
        index.add("PROJ-9", "recX")
        assert "PROJ-9" not in index._jira_to_lark and "recX" not in index._lark_to_jira
    with projects.use(1):
        index.rebuild([{"record_id": "r1", "fields": {"Jira Key": "PROJ-123"}}])
        assert index._jira_to_lark == {"PROJ-123": "r1"}  # project #1 unaffected


def test_lark_create_with_foreign_key_does_not_index_or_create(two_projects):
    with patch("lark_handler.lark_api") as ml, patch("lark_handler.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_base_tz_hours.return_value = 0.0
        ml.get_record.return_value = FOREIGN
        lark_handler.process({"action": "record_added", "record_id": "recF"}, P2_TABLE, CFG2)
        assert not mj.method_calls
    with projects.use(2):
        assert "recF" not in index._lark_to_jira


def test_lark_edit_of_foreign_row_makes_no_jira_call(two_projects):
    with patch("lark_handler.lark_api") as ml, patch("lark_handler.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_base_tz_hours.return_value = 0.0
        ml.get_record.return_value = FOREIGN
        lark_handler.process({"action": "record_edited", "record_id": "recF"}, P2_TABLE, CFG2)
        assert not mj.method_calls  # no get/update/create on project #1's issue
    with projects.use(2):
        assert "recF" not in index._lark_to_jira


def test_jira_write_guard_records_error_for_foreign_index_entry(two_projects):
    """Defence in depth: even if a foreign key got into the index, no write."""
    with projects.use(2):
        index._for()[0]["PROJ-123"] = "recF"
        index._for()[1]["recF"] = "PROJ-123"
    with patch("lark_handler.lark_api") as ml, patch("lark_handler.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_base_tz_hours.return_value = 0.0
        ml.get_record.return_value = {"record_id": "recF", "fields": {"Name": "Changed"}}
        mj.get_issue.return_value = {"fields": {"summary": "old"}}
        lark_handler.process({"action": "record_edited", "record_id": "recF"}, P2_TABLE, CFG2)
        mj.update_issue.assert_not_called()
        mj.transition_issue.assert_not_called()
        mj.move_to_sprint.assert_not_called()
    errs = _errors()
    assert len(errs) == 1 and errs[0]["jira_key"] == "PROJ-123" and errs[0]["project_id"] == 2


def test_lark_delete_of_foreign_row_makes_no_jira_call(two_projects):
    with projects.use(2):
        index._for()[0]["PROJ-123"] = "recF"
        index._for()[1]["recF"] = "PROJ-123"
    with patch("lark_handler.lark_api") as ml, patch("lark_handler.jira_api") as mj:
        ml.get_token.return_value = "tok"
        ml.get_record.side_effect = Exception("gone")
        lark_handler.process({"action": "record_deleted", "record_id": "recF"}, P2_TABLE, CFG2)
        mj.delete_issue.assert_not_called()
    assert len(_errors()) == 1


def test_jira_api_refuses_writes_to_another_projects_issue():
    with patch("jira_api.requests") as rq:
        for call in (lambda: jira_api.update_issue(CFG2, "PROJ-1", {"summary": "x"}),
                     lambda: jira_api.delete_issue(CFG2, "PROJ-1"),
                     lambda: jira_api.move_to_sprint(CFG2, 5, "PROJ-1"),
                     lambda: jira_api.transition_issue(CFG2, "PROJ-1", "Done"),
                     lambda: jira_api.move_story(CFG2, "PROJ-1", "TWO-2")):
            with pytest.raises(ValueError):
                call()
        assert not rq.method_calls


def _reconcile_mocks(ml, mj, records, issues):
    ml.get_token.return_value = "tok"
    ml.get_base_tz_hours.return_value = 0.0
    ml.find_modified_time_field.return_value = None
    ml.fetch_all_records.return_value = records
    ml.create_record.side_effect = lambda tok, b, t, f: f"new-{f.get('Jira Key')}"
    mj.fetch_all_issues.return_value = issues


def test_reconcile_never_deletes_or_counts_foreign_rows(two_projects):
    dup_foreign = {"record_id": "recF2", "fields": dict(FOREIGN["fields"])}
    with patch("reconcile.lark_api") as ml, patch("reconcile.jira_api") as mj:
        _reconcile_mocks(ml, mj, [FOREIGN, dup_foreign, OWN2], [_issue("TWO-5")])
        reconcile.run(CFG2)
        ml.delete_record.assert_not_called()  # not "Jira gone", not a duplicate
        ml.create_record.assert_not_called()
    with projects.use(2):
        assert index._jira_to_lark == {"TWO-5": "recO"}


def test_backfill_ignores_foreign_rows(two_projects):
    dup_foreign = {"record_id": "recF2", "fields": dict(FOREIGN["fields"])}
    with patch("reconcile.lark_api") as ml, patch("reconcile.jira_api") as mj:
        _reconcile_mocks(ml, mj, [FOREIGN, dup_foreign, OWN2], [_issue("TWO-5")])
        out = reconcile.backfill(CFG2)
        ml.delete_record.assert_not_called()
        mj.create_issue.assert_not_called()
        assert out["removed_duplicates"] == 0 and out["created_jira"] == 0


def test_reconcile_project1_still_deletes_its_own_gone_rows(two_projects):
    gone = {"record_id": "recG", "fields": {"Jira Key": "PROJ-77", "Title": "Gone"}}
    with patch("reconcile.lark_api") as ml, patch("reconcile.jira_api") as mj:
        _reconcile_mocks(ml, mj, [gone], [])
        reconcile.run(CFG)
        ml.delete_record.assert_called_once_with("tok", "base", "tbl", "recG")


def _check_env(monkeypatch, records):
    cols = [d["lark_field"] for d in field_mappings._DEFAULTS] + ["Name"]
    monkeypatch.setattr(lark_api, "get_token", lambda *a: "tok")
    monkeypatch.setattr(lark_api, "list_fields",
                        lambda *a: [{"field_name": c, "field_id": c} for c in cols])
    monkeypatch.setattr(lark_api, "fetch_all_records", lambda *a, **k: records)
    monkeypatch.setattr(lark_api, "get_select_options", lambda *a: [])
    monkeypatch.setattr(jira_api, "get_all_fields",
                        lambda cfg: [{"id": d["jira_field"]} for d in field_mappings._DEFAULTS])
    monkeypatch.setattr(jira_api, "get_account_ids", lambda cfg: {})


def test_check_flags_foreign_keys_and_blocks_ready(monkeypatch, two_projects):
    for k in ("JIRA_EMAIL", "JIRA_TOKEN", "JIRA_DOMAIN", "LARK_APP_ID", "LARK_APP_SECRET"):
        monkeypatch.setenv(k, "x")
    _check_env(monkeypatch, [FOREIGN, OWN2])
    out = projects_api.do_check(2)
    assert out["ready"] is False
    assert out["foreign_keys"]["count"] == 1 and out["foreign_keys"]["examples"] == ["PROJ-123"]
    assert any("PROJ-123" in f["problem"] and f["jira_field"] is None for f in out["fields"])
    _check_env(monkeypatch, [{"record_id": "r", "fields": {"Jira Key": "PROJ-1"}}])
    out1 = projects_api.do_check(1)  # project #1's own rows are fine
    assert out1["ready"] is True and out1["foreign_keys"]["count"] == 0


# ── I1: registry read failure at boot ──────────────────────────────────────

class _Broken:
    """Wrap FakeSupabase so reads of some tables fail."""

    def __init__(self, fake, fail: dict):
        self.fake, self.fail = fake, fail

    def table(self, name):
        if name in self.fail:
            raise self.fail[name]
        return self.fake.table(name)


@pytest.fixture
def registry(monkeypatch):
    for k, v in {"JIRA_PROJECT": "PROJ", "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tblEnv",
                 "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "x",
                 "LARK_APP_ID": "x", "LARK_APP_SECRET": "x"}.items():
        monkeypatch.setenv(k, v)
    saved = (projects._projects, projects._persisted, projects._fallback_reason)
    projects._projects, projects._persisted, projects._fallback_reason = {}, False, None
    yield
    projects._projects, projects._persisted, projects._fallback_reason = saved


def test_registry_failure_uses_stored_settings_and_reconcile_off(monkeypatch, fake_supabase, registry):
    fake_supabase.set_setting("active_table_id", "tblStored")
    fake_supabase.set_setting("active_table_name", "Stored")
    fake_supabase.set_setting("sync_enabled", "false")
    fake_supabase.set_setting("allowed_jira_types", '["Bug"]')
    fake_supabase.set_setting("allowed_lark_types", '["Bug", "Epic"]')
    broken = _Broken(fake_supabase, {"sync_projects": Exception("connection reset")})
    monkeypatch.setattr(history, "_get_client", lambda: broken)
    projects.load_all()
    p = projects.get(1)
    assert (p.lark_table_id, p.lark_table_name) == ("tblStored", "Stored")
    assert p.sync_enabled is False and p.reconcile_enabled is False
    assert p.allowed_jira_types == ["Bug"] and p.allowed_lark_types == ["Bug", "Epic"]
    assert projects.is_fallback()


def test_registry_and_settings_failure_keeps_reconcile_off(monkeypatch, fake_supabase, registry):
    broken = _Broken(fake_supabase, {"sync_projects": Exception("x"), "settings": Exception("y")})
    monkeypatch.setattr(history, "_get_client", lambda: broken)
    projects.load_all()
    p = projects.get(1)
    assert p.lark_table_id == "tblEnv" and p.sync_enabled is True
    assert p.reconcile_enabled is False


def test_registry_failure_keeps_previous_good_registry(monkeypatch, fake_supabase, registry):
    projects.bootstrap_default()
    projects.update(1, {"lark_table_id": "tblGood"})
    broken = _Broken(fake_supabase, {"sync_projects": Exception("blip")})
    monkeypatch.setattr(history, "_get_client", lambda: broken)
    projects.load_all()
    assert projects.get(1).lark_table_id == "tblGood" and projects.get(1).reconcile_enabled


def test_registry_missing_table_logs_migration_instruction(monkeypatch, fake_supabase, registry, caplog):
    err = Exception("{'code': 'PGRST205', 'message': \"Could not find the table "
                    "'public.sync_projects' in the schema cache\"}")
    monkeypatch.setattr(history, "_get_client", lambda: _Broken(fake_supabase, {"sync_projects": err}))
    with caplog.at_level(logging.ERROR, logger="projects"):
        projects.load_all()
    assert any("migrations/003_multi_project.sql" in r.getMessage()
               for r in caplog.records if r.levelno == logging.ERROR)


def test_env_project_requires_env(monkeypatch, registry):
    monkeypatch.delenv("LARK_TABLE_ID")
    with pytest.raises(RuntimeError, match="LARK_TABLE_ID"):
        projects._env_project()


def test_reconcile_all_skips_fallback_registry(monkeypatch, fake_supabase, registry):
    monkeypatch.setattr(history, "_get_client",
                        lambda: _Broken(fake_supabase, {"sync_projects": Exception("x")}))
    projects.load_all()
    projects._projects[1].reconcile_enabled = True  # even if it were on
    ran = []
    monkeypatch.setattr(reconcile, "run", lambda cfg: ran.append(cfg))
    main._reconcile_all()
    assert ran == []


def test_registry_retry_eventually_loads_and_rebuilds(monkeypatch, fake_supabase, registry):
    fake_supabase.set_setting("active_table_id", "tblStored")
    fail = {"sync_projects": Exception("x")}
    monkeypatch.setattr(history, "_get_client", lambda: _Broken(fake_supabase, fail))
    projects.load_all()
    assert projects.is_fallback()
    sleeps, rebuilt = [], []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            fail.clear()  # the database comes back

    async def fake_rebuild():
        rebuilt.append([p.id for p in projects.all_projects()])
    monkeypatch.setattr(main, "_initial_index_rebuild", fake_rebuild)
    monkeypatch.setattr(field_mappings, "load", lambda *a: None)
    asyncio.run(main._registry_retry_loop(sleep=fake_sleep))
    assert sleeps == [30, 60, 120]
    assert not projects.is_fallback()
    assert rebuilt == [[1]]
    assert projects.get(1).lark_table_id == "tblStored"


# ── I3: failed mapping load ────────────────────────────────────────────────

def test_failed_mapping_load_is_retried_after_backoff(monkeypatch, fake_supabase):
    for d in field_mappings._DEFAULTS:
        fake_supabase.insert_row("field_mappings", {**{k: v for k, v in d.items() if k != "id"},
                                                    "project_id": 1, "lark_field": d["lark_field"] + "!"})
    clock = [1000.0]
    monkeypatch.setattr(field_mappings.time, "time", lambda: clock[0])
    fail = {"field_mappings": Exception("timeout")}
    monkeypatch.setattr(history, "_get_client", lambda: _Broken(fake_supabase, fail))
    with projects.use(1):
        assert field_mappings.F_TITLE == "Title"  # fallback defaults
        fail.clear()
        clock[0] += 30
        assert field_mappings.F_TITLE == "Title"  # still inside the backoff
        clock[0] += 31
        assert field_mappings.F_TITLE == "Title!"  # real rows loaded on retry
        clock[0] += 3600
        assert field_mappings.F_TITLE == "Title!"


def test_skipped_live_project_records_one_error_per_hour(two_projects, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(field_mappings.time, "time", lambda: clock[0])
    field_mappings._skip_reported.clear()
    field_mappings._by_project[2] = []
    with patch("jira_handler.lark_api") as ml:
        import jira_handler
        for _ in range(3):
            jira_handler.process("jira:issue_created", _issue("TWO-1"), {}, CFG2)
        lark_handler.process({"action": "record_edited", "record_id": "r"}, P2_TABLE, CFG2)
        assert not ml.method_calls
    errs = _errors()
    assert len(errs) == 1 and errs[0]["project_id"] == 2 and "required" in errs[0]["description"]
    clock[0] += 3601
    reconcile.run(CFG2)
    assert len(_errors()) == 2


# ── I4: Jira project key validation ────────────────────────────────────────

def test_projects_create_rejects_bad_key(two_projects):
    for bad in ('X" OR project != "Y', "1AB", "A", "AB-1", "A B"):
        with pytest.raises(ValueError):
            projects.create({"jira_project_key": bad, "lark_base_token": "b9", "lark_table_id": "t9"})


def test_do_create_validates_key_and_existence(monkeypatch, two_projects):
    for k in ("JIRA_EMAIL", "JIRA_TOKEN", "JIRA_DOMAIN", "LARK_APP_ID", "LARK_APP_SECRET"):
        monkeypatch.setenv(k, "x")
    seen = []
    monkeypatch.setattr(jira_api, "get_project",
                        lambda cfg, key: seen.append((cfg["JIRA_PROJECT"], key)) or
                        ({"key": key} if key == "GOOD" else None))
    r = client.post("/api/projects", json={"jira_project_key": 'x" or 1=1',
                                           "lark_base_token": "b9", "lark_table_id": "t9"})
    assert r.status_code == 400 and seen == []
    r = client.post("/api/projects", json={"jira_project_key": "nope",
                                           "lark_base_token": "b9", "lark_table_id": "t9"})
    assert r.status_code == 400 and "NOPE" in r.json()["detail"]
    r = client.post("/api/projects", json={"jira_project_key": "good",
                                           "lark_base_token": "b9", "lark_table_id": "t9"})
    assert r.status_code == 200 and r.json()["jira_project_key"] == "GOOD"
    assert seen[-1] == ("GOOD", "GOOD")


def test_jira_project_lookup_rejects_bad_key(monkeypatch, two_projects):
    for k in ("JIRA_EMAIL", "JIRA_TOKEN", "JIRA_DOMAIN", "LARK_APP_ID", "LARK_APP_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(jira_api, "get_project", lambda cfg, key: pytest.fail("called"))
    assert client.get("/api/jira/projects/a%22b").status_code == 400


def test_fetch_all_issues_quotes_project_key():
    with patch("jira_api.requests") as rq:
        rq.post.return_value.json.return_value = {"issues": [], "isLast": True}
        rq.post.return_value.ok = True
        jira_api.fetch_all_issues(CFG2, types=["Epic"])
        jql = rq.post.call_args.kwargs["json"]["jql"]
        assert jql.startswith('project = "TWO" AND ')


# ── minors ─────────────────────────────────────────────────────────────────

def test_debug_index_and_rebuild_take_project(monkeypatch, two_projects):
    for k in ("JIRA_EMAIL", "JIRA_TOKEN", "JIRA_DOMAIN", "LARK_APP_ID", "LARK_APP_SECRET"):
        monkeypatch.setenv(k, "x")
    with projects.use(1):
        index.add("PROJ-1", "r1")
    monkeypatch.setattr(lark_api, "get_token", lambda *a: "tok")
    got = []
    monkeypatch.setattr(lark_api, "fetch_all_records",
                        lambda tok, base, table: got.append((base, table)) or [OWN2, FOREIGN])
    assert client.get("/debug/index").json()["linked_count"] == 1
    assert client.get("/debug/index?project=2").json()["linked_count"] == 0
    r = client.post("/debug/rebuild?project=2").json()
    assert r["linked_count"] == 1 and r["project"] == 2 and got == [(P2_BASE, P2_TABLE)]
    assert client.get("/debug/index?project=2").json()["jira_to_lark"] == {"TWO-5": "recO"}
    assert client.get("/debug/index").json()["jira_to_lark"] == {"PROJ-1": "r1"}
    assert client.get("/debug/index?project=99").status_code == 404


def test_wizard_has_no_inline_adddefault_onclick():
    import wizard
    src = open(wizard.__file__).read()
    assert "onclick=\"addDefault(" not in src and "addDefault(\\'" not in src
    assert "data-jira-field" in src


def test_go_live_with_table_change_rebuilds_new_table(monkeypatch, two_projects):
    p = projects.create({"jira_project_key": "THR", "lark_base_token": "b3", "lark_table_id": "t3"})
    field_mappings._by_project[p.id] = [dict(d) for d in field_mappings._DEFAULTS]
    monkeypatch.setattr(projects_api, "do_check",
                        lambda pid: {"fields": [], "people": {}, "ready": True})
    seen, bg = [], []
    monkeypatch.setattr(projects_api, "_rebuild_now",
                        lambda pid: seen.append(projects.get(pid).lark_table_id))
    monkeypatch.setattr(projects_api, "_start_rebuild", lambda pid: bg.append(pid))
    try:
        r = client.patch(f"/api/p/{p.id}", json={"setup_state": "active", "lark_table_id": "t3new"})
        assert r.status_code == 200, r.text
        assert seen == ["t3new"] and bg == []
        assert projects.get(p.id).lark_table_id == "t3new"
    finally:
        field_mappings._by_project.pop(p.id, None)


def test_go_live_failure_does_not_echo_exception(monkeypatch, two_projects):
    p = projects.create({"jira_project_key": "THR", "lark_base_token": "b3", "lark_table_id": "t3"})
    field_mappings._by_project[p.id] = [dict(d) for d in field_mappings._DEFAULTS]
    monkeypatch.setattr(projects_api, "do_check",
                        lambda pid: {"fields": [], "people": {}, "ready": True})

    def boom(pid):
        raise RuntimeError("secret-token-abc leaked")
    monkeypatch.setattr(projects_api, "_rebuild_now", boom)
    try:
        r = client.patch(f"/api/p/{p.id}", json={"setup_state": "active"})
        assert r.status_code == 400 and "secret-token-abc" not in r.text
        assert projects.get(p.id).setup_state == "draft"
    finally:
        field_mappings._by_project.pop(p.id, None)
