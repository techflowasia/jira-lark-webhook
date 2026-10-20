import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import field_mappings
import history
import lark_api
import main
import projects
import projects_api

client = TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"JIRA_PROJECT": "OLD", "LARK_BASE_TOKEN": "A", "LARK_TABLE_ID": "t1",
                 "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "x",
                 "LARK_APP_ID": "x", "LARK_APP_SECRET": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lark_api, "get_token", lambda *a: "tok")
    monkeypatch.setattr(history, "_get_client", lambda: None)
    history._fallback.clear()
    saved = (projects._projects, projects._persisted, dict(field_mappings._by_project))
    projects._projects, projects._persisted = {}, False
    projects.load_all()
    yield
    projects._projects, projects._persisted, fm = saved
    field_mappings._by_project.clear()
    field_mappings._by_project.update(fm)
    history._fallback.clear()


def _draft(key="NEW", base="B2", table="t2", name=""):
    return projects.create({"name": name or key, "jira_project_key": key,
                            "lark_base_token": base, "lark_table_id": table})


# ── pages ───────────────────────────────────────────────────────────────────

def test_new_page_is_wizard_in_sidebar_layout():
    html = client.get("/p/new").text
    assert 'class="sidebar"' in html and "New project" in html
    assert "/api/projects" in html and "resolve-link" in html


def test_setup_unknown_project_404():
    assert client.get("/p/99/setup").status_code == 404


@pytest.mark.parametrize("step", range(1, 10))
def test_every_step_renders_for_a_draft(step):
    p = _draft()
    r = client.get(f"/p/{p.id}/setup?step={step}")
    assert r.status_code == 200
    assert f"var PID = {p.id};" in r.text


def test_bad_step_clamped():
    p = _draft()
    assert client.get(f"/p/{p.id}/setup?step=zz").status_code == 200
    assert client.get(f"/p/{p.id}/setup?step=99").status_code == 200


def test_step3_shows_connect_link_and_automation_url():
    p = _draft()
    html = client.get(f"/p/{p.id}/setup?step=3").text
    assert f"/auth/start?project={p.id}" in html
    assert f"/webhook/lark-auto/{p.id}" in html


def test_step9_webhook_reminder_names_the_key_escaped():
    p = _draft(key="A<B")
    html = client.get(f"/p/{p.id}/setup?step=9").text
    assert "add <code>A&lt;B</code> to its filter in Jira" in html
    assert "<code>A<B</code>" not in html


def test_user_strings_escaped_in_setup_page():
    p = _draft(name="<script>alert(1)</script>")
    html = client.get(f"/p/{p.id}/setup?step=1").text
    assert "<script>alert(1)" not in html


# ── OAuth ───────────────────────────────────────────────────────────────────

def test_auth_start_project_state():
    _draft()
    r = client.get("/auth/start?project=2")
    assert r.status_code in (302, 307)
    q = urllib.parse.urlparse(r.headers["location"])
    assert "state=project%3A2" in q.query


def test_auth_start_legacy_and_unknown():
    r = client.get("/auth/start")
    assert "state=add_bot" in r.headers["location"]
    assert client.get("/auth/start?project=99").status_code == 404
    assert client.get("/auth/start?project=zz").status_code == 400


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()


def _fake_urlopen(monkeypatch):
    calls = []

    def urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else req
        calls.append(url)
        if "app_access_token" in url:
            return _Resp({"app_access_token": "app"})
        if "authen/v1/access_token" in url:
            return _Resp({"data": {"access_token": "usr"}})
        return _Resp({"code": 0})
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def test_callback_project_state_uses_that_projects_base(monkeypatch):
    p = _draft(base="base2")
    calls = _fake_urlopen(monkeypatch)
    r = client.get("/auth/callback?code=c&state=project:%d" % p.id)
    assert r.status_code == 200
    assert any("/drive/v1/permissions/base2/members" in c for c in calls)
    assert any("/drive/v1/files/base2/subscribe" in c for c in calls)
    assert not any("/permissions/A/" in c or "/files/A/" in c for c in calls)
    assert f"/p/{p.id}/setup?step=4" in r.text


def test_callback_legacy_state_uses_project_1(monkeypatch):
    _draft(base="base2")
    calls = _fake_urlopen(monkeypatch)
    r = client.get("/auth/callback?code=c&state=add_bot")
    assert r.status_code == 200
    assert any("/drive/v1/permissions/A/members" in c for c in calls)
    assert any("/drive/v1/files/A/subscribe" in c for c in calls)
    assert "/p/1/setup?step=4" in r.text


def test_callback_unknown_project_state_400_and_makes_no_calls(monkeypatch):
    calls = _fake_urlopen(monkeypatch)
    for st in ("project:99", "project:zz"):
        assert client.get(f"/auth/callback?code=c&state={st}").status_code == 400
    assert calls == []


# ── go live ─────────────────────────────────────────────────────────────────

def test_go_live_flips_only_that_project_and_rebuilds(monkeypatch):
    p = _draft()
    rebuilt = []
    monkeypatch.setattr(projects_api, "_start_rebuild", lambda pid: rebuilt.append(pid))
    field_mappings._by_project[p.id] = [dict(d) for d in field_mappings._DEFAULTS]
    r = client.patch(f"/api/p/{p.id}", json={"setup_state": "active", "sync_enabled": True,
                                              "reconcile_enabled": False})
    assert r.status_code == 200, r.text
    assert (projects.get(p.id).setup_state, projects.get(p.id).sync_enabled) == ("active", True)
    assert projects.get(p.id).reconcile_enabled is False
    one = projects.get(1)
    assert (one.setup_state, one.sync_enabled) == ("active", True)
    assert rebuilt == [p.id]


def test_go_live_blocked_without_required_mapping(monkeypatch):
    p = _draft()
    monkeypatch.setattr(projects_api, "_start_rebuild", lambda pid: None)
    field_mappings._by_project[p.id] = []
    r = client.patch(f"/api/p/{p.id}", json={"setup_state": "active", "sync_enabled": True})
    assert r.status_code == 400
    assert projects.get(p.id).setup_state == "draft" and not projects.get(p.id).sync_enabled


# ── R9 ──────────────────────────────────────────────────────────────────────

def test_create_uppercases_key_and_dedups_case_insensitively(monkeypatch):
    r = client.post("/api/projects", json={"jira_project_key": " abc ", "lark_base_token": "B",
                                           "lark_table_id": "t2"})
    assert r.status_code == 200 and r.json()["jira_project_key"] == "ABC"
    dup = client.post("/api/projects", json={"jira_project_key": "abc", "lark_base_token": "C",
                                             "lark_table_id": "t3"})
    assert dup.status_code == 409
    assert projects.create.__doc__
    p = projects.create({"jira_project_key": "xyz", "lark_base_token": "D", "lark_table_id": "t4"})
    assert p.jira_project_key == "XYZ"


def test_delete_draft_removes_row_and_only_its_mappings(fake_supabase):
    for d in field_mappings._DEFAULTS:
        fake_supabase.insert_row("field_mappings", {**{k: v for k, v in d.items() if k != "id"},
                                                    "project_id": 1})
    projects.load_all()
    p = projects.create({"jira_project_key": "DR", "lark_base_token": "B", "lark_table_id": "t2"})
    assert client.post(f"/api/p/{p.id}/fields/seed-defaults").status_code == 200
    r = client.delete(f"/api/p/{p.id}")
    assert r.status_code == 200
    assert projects.get(p.id) is None
    rows = fake_supabase.rows("field_mappings")
    assert len(rows) == 13 and all(x["project_id"] == 1 for x in rows)
    assert p.id not in field_mappings._by_project
    assert all(r_["id"] != p.id for r_ in fake_supabase.rows("sync_projects"))


def test_delete_refuses_active_and_project_1_and_unknown():
    p = _draft()
    projects.update(p.id, {"setup_state": "active"})
    assert client.delete(f"/api/p/{p.id}").status_code == 400
    assert projects.get(p.id) is not None
    projects.update(1, {"setup_state": "draft"})
    assert client.delete("/api/p/1").status_code == 400
    assert projects.get(1) is not None
    assert client.delete("/api/p/99").status_code == 404


def test_seed_defaults_stamps_project_and_refuses_second_time(fake_supabase):
    p = projects.create({"jira_project_key": "SD", "lark_base_token": "B", "lark_table_id": "t2"})
    r = client.post(f"/api/p/{p.id}/fields/seed-defaults")
    assert r.status_code == 200 and r.json()["seeded"] == 13
    rows = [x for x in fake_supabase.rows("field_mappings")]
    assert len(rows) == 13 and all(x["project_id"] == p.id for x in rows)
    assert client.post(f"/api/p/{p.id}/fields/seed-defaults").status_code == 409
    assert fake_supabase.count("field_mappings") == 13
    assert client.post("/api/p/99/fields/seed-defaults").status_code == 404


def test_copy_from_endpoint_copies_and_refuses_twice(fake_supabase):
    for d in field_mappings._DEFAULTS:
        fake_supabase.insert_row("field_mappings", {**{k: v for k, v in d.items() if k != "id"},
                                                    "project_id": 1})
    projects.update(1, {"assignee_map": {"A B": "Ab"}, "allowed_jira_types": ["Epic"]})
    p = projects.create({"jira_project_key": "CP", "lark_base_token": "B", "lark_table_id": "t2"})
    r = client.post(f"/api/p/{p.id}/copy-from", json={"source": 1})
    assert r.status_code == 200 and r.json()["copied_mappings"] == 13
    assert projects.get(p.id).assignee_map == {"A B": "Ab"}
    assert projects.get(p.id).allowed_jira_types == ["Epic"]
    assert client.post(f"/api/p/{p.id}/copy-from", json={"source": 1}).status_code == 409
    assert client.post(f"/api/p/{p.id}/copy-from", json={"source": 99}).status_code == 404
    assert client.post(f"/api/p/{p.id}/copy-from", json={"source": p.id}).status_code == 400


def test_resolve_link_returns_base_and_tables(monkeypatch):
    monkeypatch.setattr(lark_api, "list_tables",
                        lambda tok, base: [{"table_id": "tA", "name": "A"}, {"table_id": "tB", "name": "B"}])
    r = client.post("/api/lark/resolve-link",
                    json={"lark_base_link": "https://x.larksuite.com/base/BASE9?table=tB"})
    assert r.status_code == 200
    j = r.json()
    assert j["base_token"] == "BASE9" and j["table_id"] == "tB" and len(j["tables"]) == 2
    bad = client.post("/api/lark/resolve-link", json={"lark_base_link": "https://x.com/nope"})
    assert bad.status_code == 400
