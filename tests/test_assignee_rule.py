"""One assignee rule for every Jira->Lark path:
- unassigned: a webhook update clears the Lark Assignee; sweeps leave it (R8);
- in the project's people list: the mapped Lark name is written;
- NOT in the people list: auto-added — {displayName: displayName} joins the
  project's assignee_map, [displayName] is written to Lark, and ONE "Added"
  history row is written the first time. Nothing is cleared for that person.
Lark->Jira: an unmapped Lark assignee leaves Jira unchanged (+ warning)."""
import threading
from unittest.mock import MagicMock, patch

import pytest

import config, field_mappings, history, index, jira_handler, lark_api
import lark_handler, projects, reconcile
from tests.conftest import P2_TABLE
from tests.test_unmapped_fields import (  # noqa: F401  (fixtures)
    CFG2, _clean, _full_issue, _p2_record, four, lark_mocks, mock_lark, rec_mocks)


@pytest.fixture(autouse=True)
def _reset_warned():
    lark_handler._assignee_warned.clear()
    config._collision_warned.clear()
    yield
    lark_handler._assignee_warned.clear()
    config._collision_warned.clear()


def _warnings():
    return [r for r in history._fallback if "not in people list" in r["description"]]


def _added():
    return [r for r in history._fallback if r["description"].startswith("Added '")]


def _map():
    return dict(projects.get(2).assignee_map)


@pytest.fixture
def map_writes():
    """Every projects.update that changes assignee_map."""
    calls = []
    real = projects.update

    def spy(pid, fields):
        if "assignee_map" in fields:
            calls.append((pid, dict(fields["assignee_map"])))
        return real(pid, fields)

    with patch.object(projects, "update", side_effect=spy):
        yield calls


def _issue(key, assignee):
    issue = _full_issue(key, parent="TWO-1")
    if assignee is None:
        issue["fields"]["assignee"] = None
    else:
        issue["fields"]["assignee"] = {"displayName": assignee}
    return issue


def _rec(rid, key, assignee):
    r = _p2_record(rid, key)
    r["fields"]["Assignee"] = [{"text": assignee}] if assignee else None
    return r


def _assert_added_once(name="Bob Stranger", key="TWO-5"):
    a = _added()
    assert len(a) == 1, a
    assert a[0]["status"] == "ok" and a[0]["event"] == "config"
    assert a[0]["description"] == f"Added '{name}' to People list and Lark Assignee"
    assert a[0]["jira_key"] == key
    assert _map()[name] == name
    assert _warnings() == []


# ── config.lark_assignee_for ────────────────────────────────────────────

def test_lark_assignee_for(four, map_writes):
    with projects.use(2):
        assert config.lark_assignee_for("Ada Lovelace") == ("Ada", False)
        assert config.lark_assignee_for(None) == (None, False)
        assert config.lark_assignee_for("") == (None, False)
        assert map_writes == []
        assert config.lark_assignee_for("Nobody Known", jira_key="TWO-3") == ("Nobody Known", False)
        assert config.lark_assignee_for("Nobody Known", jira_key="TWO-3") == ("Nobody Known", False)
    assert map_writes == [(2, {"Ada Lovelace": "Ada", "Nobody Known": "Nobody Known"})]
    assert len(_added()) == 1


def test_auto_add_never_overwrites_existing_entry(four, map_writes):
    with projects.use(2):
        assert config.lark_assignee_for("Ada Lovelace") == ("Ada", False)
    assert _map() == {"Ada Lovelace": "Ada"} and map_writes == []


def test_auto_add_refuses_lark_name_already_used_by_someone_else(four, map_writes):
    """A Jira user whose displayName equals another person's Lark name would
    make Lark->Jira ambiguous (that Lark name would point at two people):
    never add it, never write or clear Lark."""
    with projects.use(2):
        assert config.lark_assignee_for("Ada", jira_key="TWO-5") == (None, True)
        assert config.lark_assignee_for("Ada", jira_key="TWO-5") == (None, True)
    assert _map() == {"Ada Lovelace": "Ada"} and map_writes == []
    assert _added() == []
    w = [r for r in history._fallback if "already the Lark name" in r["description"]]
    assert len(w) == 1 and w[0]["status"] == "skipped"


def test_auto_add_skipped_when_assignee_field_unmapped(four, map_writes):
    field_mappings._by_project[2] = [r for r in field_mappings._by_project[2]
                                     if r["jira_field"] != "assignee"]
    with projects.use(2):
        assert config.lark_assignee_for("Bob Stranger") == (None, True)
    assert map_writes == [] and _added() == []


def test_concurrent_auto_adds_keep_both_people(four):
    barrier = threading.Barrier(2)
    real_update = projects.update

    def slow_update(pid, fields):  # widen the read-merge-write window
        threading.Event().wait(0.05)
        return real_update(pid, fields)

    def worker(name):
        with projects.use(2):
            barrier.wait()
            config.lark_assignee_for(name)

    with patch.object(projects, "update", side_effect=slow_update):
        ts = [threading.Thread(target=worker, args=(n,)) for n in ("P One", "P Two")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    assert _map() == {"Ada Lovelace": "Ada", "P One": "P One", "P Two": "P Two"}
    assert len(_added()) == 2


# ── jira_handler ────────────────────────────────────────────────────────

def _update(mock_lark, issue, to_str, cur):
    with projects.use(2):
        index.add("TWO-5", "rec5")
    mock_lark.get_cached_or_fetch_record.return_value = {"fields": {
        "Title": "S TWO-5", "Assignee": [{"text": cur}] if cur else None}}
    cl = {"items": [{"field": "assignee", "fieldId": "assignee", "toString": to_str}]}
    jira_handler.process("jira:issue_updated", issue, cl, CFG2)


def test_jira_update_unmapped_auto_adds_and_writes(four, mock_lark, map_writes):
    _update(mock_lark, _issue("TWO-5", "Bob Stranger"), "Bob Stranger", cur="Ada")
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": ["Bob Stranger"]}
    _assert_added_once()
    assert len(map_writes) == 1
    # second event for the same person: mapped now — no second row/map write
    mock_lark.update_record.reset_mock()
    _update(mock_lark, _issue("TWO-5", "Bob Stranger"), "Bob Stranger", cur="Ada")
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": ["Bob Stranger"]}
    assert len(_added()) == 1 and len(map_writes) == 1


def test_jira_update_unmapped_already_in_lark_no_write(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", "Bob Stranger"), "Bob Stranger", cur="Bob Stranger")
    mock_lark.update_record.assert_not_called()
    _assert_added_once()


def test_jira_update_unassigned_clears_without_warning(four, mock_lark, map_writes):
    _update(mock_lark, _issue("TWO-5", None), None, cur="Ada")
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": None}
    assert _warnings() == [] and _added() == [] and map_writes == []


def test_jira_update_mapped_still_sets(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", "Ada Lovelace"), "Ada Lovelace", cur=None)
    assert mock_lark.update_record.call_args[0][4] == {"Assignee": ["Ada"]}
    assert _warnings() == [] and _added() == []


def test_jira_update_name_collision_leaves_lark(four, mock_lark):
    _update(mock_lark, _issue("TWO-5", "Ada"), "Ada", cur="Ada")
    mock_lark.update_record.assert_not_called()
    _update(mock_lark, _issue("TWO-5", "Ada"), "Ada", cur=None)
    mock_lark.update_record.assert_not_called()


def test_jira_create_unmapped_auto_adds(four, mock_lark, map_writes):
    jira_handler.process("jira:issue_created", _issue("TWO-6", "Bob Stranger"), {}, CFG2)
    assert mock_lark.create_record.call_args[0][3]["Assignee"] == ["Bob Stranger"]
    _assert_added_once(key="TWO-6")
    jira_handler.process("jira:issue_created", _issue("TWO-7", "Bob Stranger"), {}, CFG2)
    assert mock_lark.create_record.call_args[0][3]["Assignee"] == ["Bob Stranger"]
    assert len(_added()) == 1 and len(map_writes) == 1


def test_jira_create_unassigned_no_warning(four, mock_lark):
    jira_handler.process("jira:issue_created", _issue("TWO-6", None), {}, CFG2)
    assert "Assignee" not in mock_lark.create_record.call_args[0][3]
    assert _warnings() == [] and _added() == []


# ── reconcile ───────────────────────────────────────────────────────────

def _sweep(rec_mocks, issue, record):
    ml, mj = rec_mocks
    ml.fetch_all_records.return_value = [record]
    mj.fetch_all_issues.return_value = [issue]
    reconcile.run(CFG2)
    return ml


def _assignee_writes(ml):
    return [c[0][4]["Assignee"] for c in ml.update_record.call_args_list
            if "Assignee" in c[0][4]]


def test_reconcile_unmapped_auto_adds_and_writes(four, rec_mocks, map_writes):
    ml = _sweep(rec_mocks, _issue("TWO-5", "Bob Stranger"), _rec("rec5", "TWO-5", "Ada"))
    assert _assignee_writes(ml) == [["Bob Stranger"]]
    _assert_added_once()
    ml.update_record.reset_mock()
    _sweep(rec_mocks, _issue("TWO-5", "Bob Stranger"), _rec("rec5", "TWO-5", "Bob Stranger"))
    assert _assignee_writes(ml) == []
    assert len(_added()) == 1 and len(map_writes) == 1


def test_reconcile_unmapped_never_clears(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", "Bob Stranger"), _rec("rec5", "TWO-5", None))
    assert _assignee_writes(ml) == [["Bob Stranger"]]
    assert None not in _assignee_writes(ml)


def test_reconcile_unassigned_leaves_lark_unchanged(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", None), _rec("rec5", "TWO-5", "Ada"))
    assert _assignee_writes(ml) == []
    assert _warnings() == [] and _added() == []


def test_reconcile_mapped_person_still_set(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-5", "Ada Lovelace"), _rec("rec5", "TWO-5", None))
    assert _assignee_writes(ml) == [["Ada"]]
    assert _warnings() == [] and _added() == []


def test_reconcile_create_unmapped_auto_adds(four, rec_mocks):
    ml = _sweep(rec_mocks, _issue("TWO-9", "Bob Stranger"), _rec("rec5", "TWO-5", "Ada"))
    assert ml.create_record.call_args[0][3]["Assignee"] == ["Bob Stranger"]
    _assert_added_once(key="TWO-9")


def test_backfill_unassigned_leaves_lark_unchanged(four, rec_mocks):
    ml, mj = rec_mocks
    ml.fetch_all_records.return_value = [_rec("rec5", "TWO-5", "Ada")]
    mj.fetch_all_issues.return_value = [_issue("TWO-5", None)]
    reconcile.backfill(CFG2)
    assert _assignee_writes(ml) == []
    assert _warnings() == [] and _added() == []


def test_backfill_unmapped_auto_adds_once(four, rec_mocks, map_writes):
    ml, mj = rec_mocks
    ml.fetch_all_records.return_value = [_rec("rec5", "TWO-5", "Ada")]
    mj.fetch_all_issues.return_value = [_issue("TWO-5", "Bob Stranger")]
    reconcile.backfill(CFG2)
    assert _assignee_writes(ml) == [["Bob Stranger"]]
    _assert_added_once()
    # second pass: Lark now holds the name -> silent, no second add
    ml.update_record.reset_mock()
    ml.fetch_all_records.return_value = [_rec("rec5", "TWO-5", "Bob Stranger")]
    reconcile.backfill(CFG2)
    assert _assignee_writes(ml) == []
    assert len(_added()) == 1 and len(map_writes) == 1


def test_backfill_create_unmapped_auto_adds(four, rec_mocks):
    ml, mj = rec_mocks
    ml.fetch_all_records.return_value = []
    mj.fetch_all_issues.return_value = [_issue("TWO-9", "Bob Stranger")]
    reconcile.backfill(CFG2)
    assert ml.create_record.call_args[0][3]["Assignee"] == ["Bob Stranger"]
    _assert_added_once(key="TWO-9")


# ── lark_api: missing select option fallback ────────────────────────────

_OLD_OPTIONS = [
    {"id": "optA", "name": "Tawan", "color": 0},
    {"id": "optB", "name": "Lin", "color": 1},
    {"id": "optC", "name": "Nurse", "color": 2},
]
_FIELDS = [
    {"field_name": "Title", "field_id": "f1", "type": 1},
    {"field_name": "Jira Key", "field_id": "f2", "type": 1},
    {"field_name": "Type", "field_id": "f3", "type": 3,
     "property": {"options": [{"id": "t1", "name": "Epic", "color": 0}]}},
    {"field_name": "Assignee", "field_id": "fAss", "type": 4, "ui_type": "MultiSelect",
     "property": {"options": [dict(o) for o in _OLD_OPTIONS]}},
]


def _resp(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


@pytest.fixture
def lark_http():
    """Fake Lark HTTP. `state["reject"]` = names Lark rejects on record write
    while they are not options of the Assignee column."""
    state = {"calls": [], "options": [dict(o) for o in _OLD_OPTIONS], "reject": set()}

    def fake(method, url, **kw):
        body = kw.get("json")
        state["calls"].append((method, url, body))
        if method == "GET" and url.endswith("/fields"):
            items = [dict(f) for f in _FIELDS]
            items[-1] = dict(items[-1], property={"options": [dict(o) for o in state["options"]]})
            return _resp({"code": 0, "data": {"items": items}})
        if method == "PUT" and "/fields/" in url:
            state["options"] = [dict(o, id=o.get("id") or f"new{i}")
                                for i, o in enumerate(body["property"]["options"])]
            return _resp({"code": 0, "data": {"field": {}}})
        names = {o["name"] for o in state["options"]}
        if any(n not in names for n in (body["fields"].get("Assignee") or [])
               if n in state["reject"]):
            return _resp({"code": 1254063, "msg": "MultiSelectFieldConvFail"})
        return _resp({"code": 0, "data": {"record": {"record_id": "recX"}}})

    lark_api.invalidate_fields_cache()
    lark_api._skip_warned.clear()
    with patch("lark_api._request", side_effect=fake):
        yield state
    lark_api.invalidate_fields_cache()
    lark_api._skip_warned.clear()


def _field_puts(state):
    return [(u, b) for m, u, b in state["calls"] if m == "PUT" and "/fields/" in u]


def _record_writes(state):
    return [(m, b) for m, u, b in state["calls"] if "/records" in u]


@pytest.mark.parametrize("op", ["update", "create"])
def test_option_error_adds_option_keeping_all_old_ones_then_retries(lark_http, op):
    lark_http["reject"] = {"Bob Stranger"}
    fields = {"Title": "x", "Jira Key": "PROJ-1", "Type": "Epic",
              "Assignee": ["Bob Stranger"]}
    if op == "update":
        lark_api.update_record("tok", "bX", "tX", "rec1", fields)
    else:
        assert lark_api.create_record("tok", "bX", "tX", fields) == "recX"
    puts = _field_puts(lark_http)
    assert len(puts) == 1
    url, body = puts[0]
    assert url.endswith("/bitable/v1/apps/bX/tables/tX/fields/fAss")
    assert body["field_name"] == "Assignee" and body["type"] == 4
    sent = body["property"]["options"]
    # every existing option, with its id, name and colour, unchanged and first
    assert sent[:len(_OLD_OPTIONS)] == _OLD_OPTIONS
    assert sent[len(_OLD_OPTIONS):] == [{"name": "Bob Stranger"}]
    writes = _record_writes(lark_http)
    assert len(writes) == 2 and writes[0][1] == writes[1][1] == {"fields": fields}


def test_successful_write_never_touches_the_field(lark_http):
    lark_api.update_record("tok", "bX", "tX", "rec1", {"Assignee": ["Bob Stranger"]})
    assert _field_puts(lark_http) == []
    assert len(_record_writes(lark_http)) == 1


def test_error_with_all_options_present_reraises_without_field_put(lark_http):
    def boom(method, url, **kw):
        if method == "GET" and url.endswith("/fields"):
            return _resp({"code": 0, "data": {"items": _FIELDS}})
        if "/fields/" in url:
            raise AssertionError("field PUT must not happen")
        return _resp({"code": 99, "msg": "other failure"})

    with patch("lark_api._request", side_effect=boom):
        with pytest.raises(RuntimeError, match="other failure"):
            lark_api.update_record("tok", "bX", "tX", "rec1", {"Assignee": ["Tawan"]})


def test_option_add_failure_reraises_original_error_and_sends_nothing_else(lark_http):
    lark_http["reject"] = {"Bob Stranger"}
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url))
        if method == "GET" and url.endswith("/fields"):
            return _resp({"code": 1, "msg": "schema read failed"})
        return _resp({"code": 1254063, "msg": "MultiSelectFieldConvFail"})

    with patch("lark_api._request", side_effect=fake):
        with pytest.raises(RuntimeError, match="MultiSelectFieldConvFail"):
            lark_api.update_record("tok", "bX", "tX", "rec1", {"Assignee": ["Bob Stranger"]})
    assert not any(m == "PUT" and "/fields/" in u for m, u in calls)


# ── lark_handler (Lark -> Jira) ─────────────────────────────────────────

def _lark_update(lark_mocks, assignee):
    ml, mj = lark_mocks
    with projects.use(2):
        index.add("TWO-5", "rec5")
    ml.get_record.return_value = _rec("rec5", "TWO-5", assignee)
    mj.get_issue.return_value = {"fields": {
        "summary": "Old", "duedate": None, config.JF_START: None,
        "assignee": {"accountId": "acc-ada"}, "fixVersions": [],
        "parent": None, "status": {"name": "To Do"}, config.JF_SPRINT: []}}
    lark_handler.process({"action": "record_edited", "record_id": "rec5"}, P2_TABLE, CFG2)
    return mj


def test_lark_update_unmapped_leaves_jira_and_warns(four, lark_mocks):
    mj = _lark_update(lark_mocks, "Zed")
    for c in mj.update_issue.call_args_list:
        assert "assignee" not in c[0][2]
    w = _warnings()
    assert len(w) == 1 and w[0]["direction"] == "lark→jira" and w[0]["status"] == "skipped"
    assert w[0]["jira_key"] == "TWO-5"
    assert w[0]["description"] == ("Assignee 'Zed' not in people list "
                                   "— Jira Assignee unchanged")


def test_lark_update_mapped_person_pushes(four, lark_mocks):
    ml, mj = lark_mocks
    mj.get_account_ids.return_value = {"Ada Lovelace": "acc-ada"}
    mj = _lark_update(lark_mocks, "Ada")
    assert _warnings() == []


def test_lark_create_unmapped_creates_unassigned_and_warns(four, lark_mocks):
    ml, mj = lark_mocks
    ml.get_record.return_value = _rec("recNew", None, "Zed")
    ml.get_record.return_value["fields"]["Jira Key"] = None
    lark_handler.process({"action": "record_added", "record_id": "recNew"}, P2_TABLE, CFG2)
    assert mj.create_issue.call_args[1]["assignee_id"] is None
    assert len(_warnings()) == 1


def test_lark_second_event_on_same_record_does_not_rewarn(four, lark_mocks):
    _lark_update(lark_mocks, "Zed")
    assert len(_warnings()) == 1
    ml, mj = lark_mocks
    rec = _rec("rec5", "TWO-5", "Zed")
    rec["fields"]["Title"] = "Edited title"
    ml.get_record.return_value = rec
    lark_handler.process({"action": "record_edited", "record_id": "rec5"}, P2_TABLE, CFG2)
    assert len(_warnings()) == 1
