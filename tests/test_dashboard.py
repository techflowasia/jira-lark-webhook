import pytest
from fastapi.testclient import TestClient

import history
import main
import projects

client = TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def two(monkeypatch):
    client.cookies.clear()
    for k, v in {"JIRA_PROJECT": "OLD", "LARK_BASE_TOKEN": "A", "LARK_TABLE_ID": "t1",
                 "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "x",
                 "LARK_APP_ID": "x", "LARK_APP_SECRET": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(history, "_get_client", lambda: None)
    history._fallback.clear()
    saved = (projects._projects, projects._persisted)
    projects._projects, projects._persisted = {}, False
    projects.load_all()
    projects.update(1, {"name": "Alpha", "sync_enabled": True, "setup_state": "active"})
    projects.create({"name": "Beta <b>", "jira_project_key": "BETA",
                     "lark_base_token": "B", "lark_table_id": "t2"})
    projects.update(2, {"sync_enabled": True, "setup_state": "active"})
    for pid, key, desc in ((1, "AAA-1", "first project row"), (2, "BBB-2", "second project row")):
        with projects.use(pid):
            history.record(direction="jira→lark", event="updated", jira_key=key,
                           description=desc)
    yield
    history._fallback.clear()
    projects._projects, projects._persisted = saved


def test_project_page_is_scoped_to_its_project():
    html = client.get("/p/2").text
    assert "Alpha" in html and "Beta &lt;b&gt;" in html
    assert "/api/p/2/" in html and "/api/p/1/" not in html
    assert "/p/2/toggle" in html
    assert "second project row" in html and "first project row" not in html


def test_unknown_project_404():
    assert client.get("/p/99").status_code == 404


def test_project_page_sets_last_viewed_cookie():
    r = client.get("/p/2")
    assert "lp=2" in r.headers.get("set-cookie", "")


def test_root_redirects_to_first_without_cookie():
    r = client.get("/")
    assert r.status_code in (302, 303, 307) and r.headers["location"] == "/p/1"


def test_root_redirects_to_last_viewed_and_ignores_bad_cookie():
    assert client.get("/", cookies={"lp": "2"}).headers["location"] == "/p/2"
    assert client.get("/", cookies={"lp": "99"}).headers["location"] == "/p/1"
    assert client.get("/", cookies={"lp": "zz"}).headers["location"] == "/p/1"


def test_activity_filters_by_project_and_shows_project_column():
    html = client.get("/activity?project=2").text
    assert "second project row" in html and "first project row" not in html
    both = client.get("/activity").text
    assert "second project row" in both and "first project row" in both
    assert "<th>Project</th>" in both and "Alpha" in both


def test_history_text_is_escaped():
    with projects.use(1):
        history.record(direction="system", event="config", jira_key="X-1",
                       description="<script>alert(1)</script>", status="error",
                       error="<img src=x onerror=1>")
    for url in ("/p/1", "/activity"):
        html = client.get(url).text
        assert "<script>alert(1)" not in html and "<img src=x" not in html
        assert "&lt;script&gt;alert(1)" in html


def test_sidebar_has_health_new_project_and_activity_links():
    html = client.get("/p/1").text
    assert "/p/new" in html and "/activity" in html and "/toggle/lark-value-cache" in html
    assert "#22c55e" in html  # ok dot for Alpha
    assert "max-width: 767px" in html


def test_draft_project_shows_setup_banner():
    projects.update(2, {"setup_state": "draft"})
    html = client.get("/p/2").text
    assert "/p/2/setup" in html
    assert "/p/2/setup" not in client.get("/p/1").text


def test_no_unscoped_api_urls_in_project_page():
    import re
    html = client.get("/p/2").text
    assert not re.findall(r"'/api/(?!p/)", html)
    assert "API + 'jira-fields'" in html


def test_new_and_setup_routes_not_shadowed():
    assert client.get("/p/new").status_code == 200
    assert client.get("/p/2/setup").status_code == 200


def test_activity_hides_lark_column_by_class_and_project_filter_keeps_custom_dates():
    html = client.get("/activity?range=custom&from_date=2026-01-01&to_date=2026-02-01").text
    assert 'th class="col-lark"' in html and "nth-child(6)" not in html
    form = html.split('class="proj-filter"')[1].split("</form>")[0]
    assert 'name="from_date"' in form and 'name="to_date"' in form
