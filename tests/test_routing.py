import pytest
from fastapi.testclient import TestClient

import history
import jira_handler
import lark_handler
import main
import projects
import reconcile
import routing


@pytest.fixture(autouse=True)
def two_projects(monkeypatch):
    for k, v in {"JIRA_PROJECT": "OLD", "LARK_BASE_TOKEN": "A", "LARK_TABLE_ID": "t1",
                 "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "x",
                 "LARK_APP_ID": "x", "LARK_APP_SECRET": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(history, "_get_client", lambda: None)
    projects._projects = {}
    projects._persisted = False
    projects.load_all()
    projects.update(1, {"sync_enabled": True, "setup_state": "active"})
    projects.create({"jira_project_key": "NEW", "lark_base_token": "B", "lark_table_id": "t2"})
    projects.update(2, {"sync_enabled": True, "setup_state": "active"})
    main._raw_payloads.clear()
    yield
    projects._projects = {}
    projects._persisted = False


@pytest.fixture
def calls(monkeypatch):
    got = {"jira": [], "lark": []}
    monkeypatch.setattr(jira_handler, "process", lambda *a: got["jira"].append(a))
    monkeypatch.setattr(lark_handler, "process", lambda *a: got["lark"].append(a))
    return got


client = TestClient(main.app)


def _jira_body(key, project=True):
    issue = {"key": key, "fields": {"project": {"key": key.rsplit("-", 1)[0]}} if project else {}}
    return {"webhookEvent": "jira:issue_updated", "issue": issue, "changelog": {}}


def test_route_jira_by_field_and_fallback():
    assert routing.route_jira(_jira_body("NEW-3")).id == 2
    assert routing.route_jira(_jira_body("NEW-3", project=False)).id == 2
    assert routing.route_jira(_jira_body("ZZZ-1")) is None
    assert routing.route_jira({}) is None


def test_route_lark_event():
    assert routing.route_lark_event({"file_token": "B", "table_id": "t2"}).id == 2
    assert routing.route_lark_event({"file_token": "B", "table_id": "t1"}) is None
    assert routing.route_lark_event({}) is None


def test_route_lark_auto():
    assert routing.route_lark_auto({}, None).id == 1
    assert routing.route_lark_auto({"table_id": "t1"}, None).id == 1
    assert routing.route_lark_auto({"table_id": "t2"}, None) is None  # other base
    assert routing.route_lark_auto({"table_id": "t1"}, 2) is None
    assert routing.route_lark_auto({"table_id": "t2"}, 2).id == 2
    assert routing.route_lark_auto({}, 2).id == 2
    assert routing.route_lark_auto({}, 99) is None


def test_is_live():
    assert routing.is_live(projects.get(1))
    assert not routing.is_live(None)
    projects.update(2, {"setup_state": "draft"})
    assert not routing.is_live(projects.get(2))
    projects.update(2, {"setup_state": "active", "sync_enabled": False})
    assert not routing.is_live(projects.get(2))


def test_jira_webhook_routes_to_project_2(calls):
    r = client.post("/webhook/jira", json=_jira_body("NEW-3"))
    assert r.json() == {"ok": True}
    assert len(calls["jira"]) == 1 and calls["jira"][0][-1]["PROJECT_ID"] == 2


def test_jira_unknown_project_not_scheduled_but_captured(calls):
    r = client.post("/webhook/jira", json=_jira_body("ZZZ-1"))
    assert r.json()["note"] == "no live project"
    assert calls["jira"] == []
    assert main._raw_payloads[0]["routed"] is None


def test_draft_or_off_not_scheduled(calls):
    projects.update(2, {"setup_state": "draft"})
    client.post("/webhook/jira", json=_jira_body("NEW-3"))
    projects.update(2, {"setup_state": "active", "sync_enabled": False})
    client.post("/webhook/jira", json=_jira_body("NEW-3"))
    assert calls["jira"] == []


def test_lark_event_routes_cfg(calls):
    body = {"event": {"file_token": "B", "table_id": "t2",
                      "action_list": [{"action": "record_added", "record_id": "r"}]}}
    client.post("/webhook/lark", json=body)
    assert len(calls["lark"]) == 1
    action, table, cfg = calls["lark"][0]
    assert table == "t2" and cfg["PROJECT_ID"] == 2 and cfg["LARK_BASE_TOKEN"] == "B"


def test_lark_url_verification_before_routing(calls):
    r = client.post("/webhook/lark", json={"type": "url_verification", "challenge": "c"})
    assert r.json() == {"challenge": "c"}


def test_lark_auto_legacy_and_by_id(calls):
    client.post("/webhook/lark-auto", json={"action": "record_edited", "record_id": "r"})
    assert calls["lark"][-1][2]["PROJECT_ID"] == 1 and calls["lark"][-1][1] == "t1"
    n = len(calls["lark"])
    r = client.post("/webhook/lark-auto/2", json={"action": "record_edited",
                                                  "record_id": "r", "table_id": "t1"})
    assert r.json()["note"] == "no live project" and len(calls["lark"]) == n
    client.post("/webhook/lark-auto/2", json={"action": "record_edited", "record_id": "r"})
    assert calls["lark"][-1][2]["PROJECT_ID"] == 2 and calls["lark"][-1][1] == "t2"


def test_toggle_project_2_leaves_project_1():
    r = client.post("/p/2/toggle", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/p/2"
    assert projects.get(2).sync_enabled is False
    assert projects.get(1).sync_enabled is True
    client.post("/p/2/toggle/reconcile", follow_redirects=False)
    assert projects.get(2).reconcile_enabled is True
    before = projects.get(1).reconcile_enabled
    client.post("/p/2/toggle/reconcile", follow_redirects=False)
    assert projects.get(1).reconcile_enabled == before


def test_legacy_toggle_acts_on_project_1():
    client.post("/toggle", follow_redirects=False)
    assert projects.get(1).sync_enabled is False and projects.get(2).sync_enabled is True


def test_health_lists_projects():
    j = client.get("/health").json()
    assert j["ok"] is True
    assert {"id": 2, "sync_enabled": True} in j["projects"]


def test_reconcile_loop_isolates_failures(monkeypatch):
    projects.update(1, {"reconcile_enabled": True})
    projects.update(2, {"reconcile_enabled": True})
    ran = []

    def fake(cfg):
        ran.append(cfg["PROJECT_ID"])
        if cfg["PROJECT_ID"] == 1:
            raise RuntimeError("boom")
    monkeypatch.setattr(reconcile, "run", fake)
    main._reconcile_all()
    assert ran == [1, 2]


def test_reconcile_skips_draft_and_disabled(monkeypatch):
    projects.update(1, {"reconcile_enabled": False})
    projects.update(2, {"reconcile_enabled": True, "setup_state": "draft"})
    ran = []
    monkeypatch.setattr(reconcile, "run", lambda cfg: ran.append(cfg["PROJECT_ID"]))
    main._reconcile_all()
    assert ran == []
