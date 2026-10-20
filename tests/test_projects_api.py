import pytest
from fastapi.testclient import TestClient

import field_mappings
import history
import jira_api
import lark_api
import main
import projects
import projects_api
import reconcile

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"JIRA_PROJECT": "OLD", "LARK_BASE_TOKEN": "A", "LARK_TABLE_ID": "t1",
                 "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "x",
                 "LARK_APP_ID": "x", "LARK_APP_SECRET": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lark_api, "get_token", lambda *a: "tok")
    monkeypatch.setattr(projects_api, "_start_rebuild", lambda pid: None)
    saved = (projects._projects, projects._persisted)
    projects._projects, projects._persisted = {}, False
    projects.load_all()
    yield
    projects._projects, projects._persisted = saved


def _seed_defaults(fake, pid):
    for d in field_mappings._DEFAULTS:
        fake.insert_row("field_mappings", {**{k: v for k, v in d.items() if k != "id"},
                                           "project_id": pid})
    field_mappings.load(pid)


def test_create_is_draft_off_and_duplicate_409():
    r = client.post("/api/projects", json={"jira_project_key": "NEW", "lark_base_token": "B",
                                           "lark_table_id": "t2"})
    assert r.status_code == 200
    p = projects.get(r.json()["id"])
    assert (p.setup_state, p.sync_enabled, p.reconcile_enabled) == ("draft", False, False)
    dup = client.post("/api/projects", json={"jira_project_key": "NEW", "lark_base_token": "C",
                                             "lark_table_id": "t9"})
    assert dup.status_code == 409
    dup2 = client.post("/api/projects", json={"jira_project_key": "OTHER", "lark_base_token": "B",
                                              "lark_table_id": "t2"})
    assert dup2.status_code == 409


def test_create_from_link(monkeypatch):
    r = client.post("/api/projects", json={
        "jira_project_key": "LNK",
        "lark_base_link": "https://x.larksuite.com/base/BASE9?table=tblZ&view=v"})
    assert r.status_code == 200
    p = projects.get(r.json()["id"])
    assert (p.lark_base_token, p.lark_table_id) == ("BASE9", "tblZ")


def test_create_copy_from_copies_13_and_leaves_source(fake_supabase):
    _seed_defaults(fake_supabase, 1)
    projects.update(1, {"assignee_map": {"A B": "Ab"}, "allowed_jira_types": ["Epic"]})
    before = fake_supabase.rows("field_mappings")
    r = client.post("/api/projects", json={"jira_project_key": "CP", "lark_base_token": "B",
                                           "lark_table_id": "t2", "copy_from": 1})
    assert r.status_code == 200
    pid = r.json()["id"]
    rows = fake_supabase.rows("field_mappings")
    assert len([x for x in rows if x["project_id"] == pid]) == 13
    assert [x for x in rows if x["project_id"] == 1] == before
    p = projects.get(pid)
    assert p.assignee_map == {"A B": "Ab"} and p.allowed_jira_types == ["Epic"]
    assert p.setup_state == "draft" and not p.sync_enabled


def test_copy_from_refuses_non_empty_destination(fake_supabase):
    _seed_defaults(fake_supabase, 1)
    p2 = projects.create({"jira_project_key": "CP", "lark_base_token": "B", "lark_table_id": "t2"})
    assert field_mappings.copy_from(1, p2.id) == 13
    with pytest.raises(ValueError):
        field_mappings.copy_from(1, p2.id)
    assert len([x for x in fake_supabase.rows("field_mappings") if x["project_id"] == p2.id]) == 13


def test_copy_from_unknown_source_404():
    r = client.post("/api/projects", json={"jira_project_key": "CP", "lark_base_token": "B",
                                           "lark_table_id": "t2", "copy_from": 99})
    assert r.status_code == 404
    assert projects.find_by_jira_key("CP") is None


def test_unknown_pid_404():
    for path in ("/api/p/99/fields", "/api/p/99/check", "/api/p/99/people", "/api/p/99/sync-types"):
        assert client.get(path).status_code == 404
    assert client.patch("/api/p/99", json={"name": "x"}).status_code == 404
    assert client.post("/api/p/99/backfill").status_code == 404


def test_list_projects_health(monkeypatch):
    p2 = projects.create({"jira_project_key": "NEW", "lark_base_token": "B", "lark_table_id": "t2"})
    p3 = projects.create({"jira_project_key": "THR", "lark_base_token": "C", "lark_table_id": "t3"})
    projects.update(1, {"sync_enabled": True, "setup_state": "active"})
    projects.update(p2.id, {"sync_enabled": True, "setup_state": "active"})
    projects.update(p3.id, {"setup_state": "active", "sync_enabled": False})
    seen = {}

    def err(pid, since):
        seen[pid] = since
        return 3 if pid == p2.id else 0
    monkeypatch.setattr(history, "error_count", err)
    got = {x["id"]: x for x in client.get("/api/projects").json()}
    assert got[1]["health"] == "ok"
    assert got[p2.id]["health"] == "error"
    assert got[p3.id]["health"] == "paused"
    assert got[1]["jira_project_key"] == "OLD" and got[1]["sync_enabled"] is True
    assert all(s.tzinfo is not None for s in seen.values())
    projects.update(p2.id, {"setup_state": "draft"})
    assert {x["id"]: x for x in client.get("/api/projects").json()}[p2.id]["health"] == "draft"


def test_patch_project(monkeypatch):
    rebuilt = []
    monkeypatch.setattr(projects_api, "_start_rebuild", lambda pid: rebuilt.append(pid))
    r = client.patch("/api/p/1", json={"name": "Renamed", "jira_board_id": 7,
                                       "assignee_map": {"A": "B"},
                                       "allowed_jira_types": ["Epic"], "allowed_lark_types": ["Epic"]})
    assert r.status_code == 200
    p = projects.get(1)
    assert (p.name, p.jira_board_id, p.assignee_map, p.allowed_jira_types) == \
        ("Renamed", "7", {"A": "B"}, ["Epic"])
    assert rebuilt == []
    assert client.patch("/api/p/1", json={"lark_table_id": "t7", "lark_table_name": "T7"}).status_code == 200
    assert projects.get(1).lark_table_id == "t7" and rebuilt == [1]
    # base token / jira key are not patchable
    client.patch("/api/p/1", json={"lark_base_token": "ZZ", "jira_project_key": "ZZ"})
    assert projects.get(1).lark_base_token == "A" and projects.get(1).jira_project_key == "OLD"
    assert client.patch("/api/p/1", json={"assignee_map": {"A": 1}}).status_code == 400
    assert client.patch("/api/p/1", json={"setup_state": "bogus"}).status_code == 400


def test_patch_table_conflict_409():
    projects.create({"jira_project_key": "NEW", "lark_base_token": "A", "lark_table_id": "t2"})
    assert client.patch("/api/p/1", json={"lark_table_id": "t2"}).status_code == 409
    assert projects.get(1).lark_table_id == "t1"


def test_activate_requires_mappings(monkeypatch):
    p2 = projects.create({"jira_project_key": "NEW", "lark_base_token": "B", "lark_table_id": "t2"})
    field_mappings._by_project[p2.id] = []
    assert client.patch(f"/api/p/{p2.id}", json={"setup_state": "active"}).status_code == 400
    assert projects.get(p2.id).setup_state == "draft"


def test_delete_required_mapping_400(fake_supabase):
    _seed_defaults(fake_supabase, 1)
    rows = fake_supabase.rows("field_mappings")
    title = next(r for r in rows if r["jira_field"] == "summary")
    r = client.delete(f"/api/p/1/fields/{title['id']}")
    assert r.status_code == 400 and "required" in r.json()["error"]
    assert any(x["id"] == title["id"] for x in fake_supabase.rows("field_mappings"))
    status = next(r for r in rows if r["jira_field"] == "status")
    assert client.delete(f"/api/p/1/fields/{status['id']}").status_code == 200


def test_fields_scoped_by_project(fake_supabase):
    _seed_defaults(fake_supabase, 1)
    p2 = projects.create({"jira_project_key": "NEW", "lark_base_token": "B", "lark_table_id": "t2"})
    assert len(client.get("/api/p/1/fields").json()["mappings"]) == 13
    assert client.get(f"/api/p/{p2.id}/fields").json()["mappings"] == []
    r = client.post(f"/api/p/{p2.id}/fields", json={"lark_field": "Foo", "jira_field": "customfield_9",
                                                    "direction": "both", "field_type": "text",
                                                    "active": True})
    assert r.json()["ok"] is True
    assert len(client.get("/api/p/1/fields").json()["mappings"]) == 13


def _check_env(monkeypatch, lark_cols, jira_ids, accounts=None, options=None):
    monkeypatch.setattr(lark_api, "list_fields",
                        lambda *a: [{"field_name": c, "field_id": c} for c in lark_cols])
    monkeypatch.setattr(jira_api, "get_all_fields",
                        lambda cfg: [{"id": i, "name": i, "custom": True} for i in jira_ids])
    monkeypatch.setattr(jira_api, "get_account_ids", lambda cfg: accounts or {})
    monkeypatch.setattr(lark_api, "get_select_options", lambda *a: options or [])


def test_check_flags_missing_lark_column_and_jira_field(monkeypatch):
    cols = [d["lark_field"] for d in field_mappings._DEFAULTS if d["lark_field"] != "R. MD"]
    ids = [d["jira_field"] for d in field_mappings._DEFAULTS
           if d["jira_field"].startswith("customfield_") and d["jira_field"] != "customfield_10176"]
    _check_env(monkeypatch, cols, ids)
    body = client.get("/api/p/1/check").json()
    probs = {(f["lark_field"], f["jira_field"]): f["problem"] for f in body["fields"]}
    assert any(k[0] == "R. MD" for k in probs)
    assert any(k[1] == "customfield_10176" for k in probs)
    assert body["ready"] is False
    assert len(body["fields"]) == 2


def test_check_ready_when_clean_and_people(monkeypatch):
    cols = [d["lark_field"] for d in field_mappings._DEFAULTS]
    ids = [d["jira_field"] for d in field_mappings._DEFAULTS]
    projects.update(1, {"assignee_map": {"Ann": "Ann", "Bob": "Bobby"}})
    _check_env(monkeypatch, cols, ids, accounts={"Ann": "1", "Cat": "2"}, options=["Ann"])
    body = client.get("/api/p/1/check").json()
    assert body["fields"] == [] and body["ready"] is True
    assert body["people"] == {"unmapped_jira": ["Cat"], "unknown_lark": ["Bobby"]}


def test_check_surfaces_missing_required_mapping(monkeypatch):
    cols = [d["lark_field"] for d in field_mappings._DEFAULTS]
    ids = [d["jira_field"] for d in field_mappings._DEFAULTS]
    _check_env(monkeypatch, cols, ids)
    field_mappings._by_project[1] = [dict(d) for d in field_mappings._DEFAULTS
                                     if d["jira_field"] != "summary"]
    body = client.get("/api/p/1/check").json()
    assert body["ready"] is False
    assert any(f["jira_field"] == "summary" and "required" in f["problem"] for f in body["fields"])


def test_check_makes_no_writes(monkeypatch):
    cols = [d["lark_field"] for d in field_mappings._DEFAULTS]
    _check_env(monkeypatch, cols, [])
    for name in ("create_record", "update_record", "delete_record"):
        monkeypatch.setattr(lark_api, name, lambda *a, **k: pytest.fail("write"))
    for name in ("create_issue", "update_issue", "delete_issue"):
        monkeypatch.setattr(jira_api, name, lambda *a, **k: pytest.fail("write"))
    assert client.get("/api/p/1/check").status_code == 200


def test_people_get_put():
    projects.update(1, {"assignee_map": {"A": "a"}})
    assert client.get("/api/p/1/people").json() == {"assignee_map": {"A": "a"}}
    assert client.put("/api/p/1/people", json={"assignee_map": {"X": "y"}}).status_code == 200
    assert projects.get(1).assignee_map == {"X": "y"}
    assert client.put("/api/p/1/people", json={"assignee_map": {"X": 3}}).status_code == 400
    assert client.put("/api/p/1/people", json={"assignee_map": ["x"]}).status_code == 400
    assert projects.get(1).assignee_map == {"X": "y"}


def test_sync_types_scoped():
    p2 = projects.create({"jira_project_key": "NEW", "lark_base_token": "B", "lark_table_id": "t2"})
    r = client.post(f"/api/p/{p2.id}/sync-types", json={"jira_types": ["Bug"], "lark_types": ["Bug"]})
    assert r.json()["ok"] is True
    assert client.get(f"/api/p/{p2.id}/sync-types").json()["allowed_jira"] == ["Bug"]
    assert client.get("/api/p/1/sync-types").json()["allowed_jira"] != ["Bug"]


def test_backfill_scoped(monkeypatch):
    p2 = projects.create({"jira_project_key": "NEW", "lark_base_token": "B", "lark_table_id": "t2"})
    got = []
    monkeypatch.setattr(reconcile, "backfill", lambda cfg: got.append(cfg) or {"matched": 0})
    field_mappings._by_project[p2.id] = [dict(d) for d in field_mappings._DEFAULTS]
    assert client.post(f"/api/p/{p2.id}/backfill").json() == {"matched": 0}
    assert got[0]["JIRA_PROJECT"] == "NEW" and got[0]["LARK_TABLE_ID"] == "t2"


def test_backfill_refused_when_required_mapping_missing(monkeypatch):
    monkeypatch.setattr(reconcile, "backfill", lambda cfg: pytest.fail("must not run"))
    field_mappings._by_project[1] = []
    r = client.post("/api/p/1/backfill").json()
    assert "required" in r["error"]


def test_legacy_aliases_match_scoped(monkeypatch, fake_supabase):
    _seed_defaults(fake_supabase, 1)
    monkeypatch.setattr(lark_api, "list_tables", lambda *a: [{"table_id": "t1", "name": "T"}])
    monkeypatch.setattr(lark_api, "list_fields", lambda *a: [{"field_name": "Title", "field_id": "f"}])
    monkeypatch.setattr(jira_api, "get_project_issue_types", lambda cfg: ["Epic"])
    monkeypatch.setattr(lark_api, "get_select_options", lambda *a: ["Epic"])
    for legacy, scoped in (("/api/tables", "/api/p/1/tables"), ("/api/fields", "/api/p/1/fields"),
                           ("/api/lark-fields", "/api/p/1/lark-fields"),
                           ("/api/jira-issue-types", "/api/p/1/jira-issue-types"),
                           ("/api/lark-type-options", "/api/p/1/lark-type-options"),
                           ("/api/sync-types", "/api/p/1/sync-types")):
        a, b = client.get(legacy), client.get(scoped)
        assert a.status_code == 200 and a.json() == b.json(), legacy


def test_legacy_set_table_updates_project_1(monkeypatch):
    rebuilt = []
    monkeypatch.setattr(projects_api, "_start_rebuild", lambda pid: rebuilt.append(pid))
    r = client.post("/settings/table", json={"table_id": "t5", "name": "Five"})
    assert r.json()["ok"] is True
    assert projects.get(1).lark_table_id == "t5" and projects.get(1).lark_table_name == "Five"
    assert rebuilt == [1]
    assert client.post("/settings/table", json={}).json()["ok"] is False


def test_legacy_delete_required_keeps_shape(fake_supabase):
    _seed_defaults(fake_supabase, 1)
    title = next(r for r in fake_supabase.rows("field_mappings") if r["jira_field"] == "summary")
    out = client.delete(f"/settings/fields/{title['id']}").json()
    assert out["ok"] is False and "required" in out["error"]


def test_jira_project_lookup(monkeypatch):
    monkeypatch.setattr(jira_api, "get_project", lambda cfg, key: (
        {"name": "Proj", "issueTypes": [{"name": "Epic"}, {"name": "Task"}]} if key == "ABC" else None))
    monkeypatch.setattr(jira_api, "get_boards", lambda cfg, key: [{"id": 5, "name": "Board"}])
    ok = client.get("/api/jira/projects/ABC").json()
    assert ok == {"exists": True, "name": "Proj", "boards": [{"id": 5, "name": "Board"}],
                  "issue_types": ["Epic", "Task"]}
    assert client.get("/api/jira/projects/NOPE").json() == {
        "exists": False, "name": None, "boards": [], "issue_types": []}


def test_parse_base_link(monkeypatch):
    assert lark_api.parse_base_link("tok", "https://a.larksuite.com/base/Bxyz?table=tblA&view=v") == ("Bxyz", "tblA")
    assert lark_api.parse_base_link("tok", "https://a.larksuite.com/base/Bxyz") == ("Bxyz", None)

    class R:
        ok = True
        status_code = 200

        def __init__(self, obj_type):
            self.obj_type = obj_type

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": 0, "data": {"node": {"obj_type": self.obj_type, "obj_token": "BAPP"}}}
    seen = {}

    def fake(method, url, **kw):
        seen["url"] = url
        seen["params"] = kw.get("params")
        return R("bitable")
    monkeypatch.setattr(lark_api, "_request", fake)
    assert lark_api.parse_base_link("tok", "https://a.larksuite.com/wiki/NODE1?table=tblW") == ("BAPP", "tblW")
    assert "wiki/v2/spaces/get_node" in seen["url"] and seen["params"] == {"token": "NODE1"}
    monkeypatch.setattr(lark_api, "_request", lambda *a, **k: R("docx"))
    with pytest.raises(ValueError):
        lark_api.parse_base_link("tok", "https://a.larksuite.com/wiki/NODE1")
    with pytest.raises(ValueError, match="Not a Lark Base link"):
        lark_api.parse_base_link("tok", "https://example.com/hello")
    with pytest.raises(ValueError, match="Not a Lark Base link"):
        lark_api.parse_base_link("tok", "garbage")
