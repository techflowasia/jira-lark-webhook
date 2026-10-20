"""People editor UI (dashboard People section, wizard step 6): a person can be
marked ignored (Lark name ""), ignored rows show "(ignored)", and saving never
silently drops a row whose Lark name is empty. The page's own JS functions are
extracted from the served HTML and run in node."""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import main
import projects
from tests.test_wizard import _draft, env  # noqa: F401  (autouse fixture)

client = TestClient(main.app, follow_redirects=False)
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node not installed")


def _fn(html: str, name: str) -> str:
    """Source of `function name(...) {...}` from the page (brace-matched)."""
    start = html.index(f"function {name}(")
    i = html.index("{", start)
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start:j + 1]
    raise AssertionError(f"unbalanced {name}")


def _run(html: str, names: list, body: str):
    src = "\n".join(_fn(html, n) for n in names) + "\n" + body
    out = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── dashboard ───────────────────────────────────────────────────────────

def _dash():
    projects.update(1, {"setup_state": "active"})
    return client.get("/p/1").text


def test_dashboard_renders_ignored_row():
    html = _dash()
    assert "<th>Ignore</th>" in html
    r = _run(html, ["esc", "peopleRow"],
             "console.log(JSON.stringify([peopleRow('Ivy', '', true), peopleRow('Ada L', 'Ada', false)]))")
    ign, normal = r
    assert '<span class="pp-ign-tag"> (ignored)</span>' in ign      # visible
    assert "pp-ign" in ign and "checked" in ign and "disabled" in ign
    assert '<span class="pp-ign-tag" style="display:none"> (ignored)</span>' in normal  # hidden
    assert "checked" not in normal and "disabled" not in normal and 'value="Ada"' in normal


def test_dashboard_collect_keeps_ignored_and_refuses_missing_names():
    html = _dash()
    r = _run(html, ["collectPeople"], """
      console.log(JSON.stringify([
        collectPeople([{j: 'Ada L', l: 'Ada', ign: false}, {j: 'Ivy', l: '', ign: true},
                       {j: 'Old', l: 'Was', ign: true}, {j: '', l: '', ign: false}]),
        collectPeople([{j: 'Bob', l: '', ign: false}, {j: 'Ada L', l: 'Ada', ign: false}])
      ]))""")
    ok, bad = r
    assert ok == {"map": {"Ada L": "Ada", "Ivy": "", "Old": ""}, "missing": []}
    assert bad["missing"] == ["Bob"]


def test_dashboard_save_aborts_when_a_name_is_missing():
    html = _dash()
    save = _fn(html, "savePeople")
    assert "collectPeople(" in save
    assert save.index("missing.length") < save.index("fetch(")


# ── wizard step 6 ───────────────────────────────────────────────────────

def _wiz():
    p = _draft()
    return client.get(f"/p/{p.id}/setup?step=6").text


def test_wizard_renders_ignored_row_and_ignore_checkbox():
    html = _wiz()
    assert 'id="pign"' in html
    r = _run(html, ["esc", "personRow"],
             "console.log(JSON.stringify([personRow('Ivy', ''), personRow('Ada L', 'Ada')]))")
    ign, normal = r
    assert "(ignored)" in ign and "Ivy" in ign
    assert "(ignored)" not in normal and "Ada" in normal


def test_wizard_add_person_ignored_and_missing_name():
    html = _wiz()
    r = _run(html, ["addPersonTo"], """
      var P = {};
      var e1 = addPersonTo(P, 'Ivy', '', true);
      var e2 = addPersonTo(P, 'Ada L', 'Ada', false);
      var e3 = addPersonTo(P, 'Bob', '', false);
      console.log(JSON.stringify({P: P, errs: [e1, e2, e3]}))""")
    assert r["P"] == {"Ivy": "", "Ada L": "Ada"}
    assert r["errs"][0] == "" and r["errs"][1] == "" and r["errs"][2]
