"""Tests for the in-memory Lark record cache on the Jira→Lark update path.

The cache eliminates the ~1 get_record call/event on the Jira→Lark hot path
(jira_handler._handle_update line 119) — the dominant remaining consumer of
the Lark Basic 10k/month quota after prior optimizations (see CHANGELOG
entries 599c342, 44ee5a3, 672e4b4).

These tests use surgical patches on lark_api functions (not the whole module)
so the real lark_api._record_cache participates in the test path.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import patch, MagicMock
import lark_api, dedup, index


def _fake_get_record_response(record_id: str, fields: dict):
    """Build a fake Lark API response for GET /records/{id}."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"code": 0, "data": {"record": {
        "record_id": record_id, "fields": fields}}}
    return resp

CFG = {
    "JIRA_EMAIL": "x", "JIRA_TOKEN": "x", "JIRA_DOMAIN": "test.atlassian.net",
    "JIRA_PROJECT": "PROJ", "LARK_APP_ID": "x", "LARK_APP_SECRET": "x",
    "LARK_BASE_TOKEN": "base", "LARK_TABLE_ID": "tbl",
}

ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "New title",
        "issuetype": {"name": "Epic"},
        "assignee": None,
        "customfield_10016": None,
        "customfield_10175": None,
        "customfield_10176": None,
        "status": {"name": "To Do"},
        "parent": None,
    }
}


def setup_function():
    dedup._cache.clear()
    index._jira_to_lark.clear()
    index._lark_to_jira.clear()
    lark_api._record_cache.clear()


def test_cache_hit_skips_get_record():
    """Fresh cache entry → 0 get_record calls; write computed against cached values."""
    index._jira_to_lark["PROJ-1"] = "recABC"
    index._lark_to_jira["recABC"] = "PROJ-1"
    # Pre-populate cache with the same shape get_record returns
    lark_api._record_cache["recABC"] = {
        "fields": {"Title": "Old title"},
        "expires_at": time.time() + 60,
    }

    changelog = {"items": [{"field": "summary", "toString": "New title", "to": None}]}
    with patch("lark_api.get_token", return_value="tok"), \
         patch("lark_api.get_record") as mock_get, \
         patch("lark_api.update_record") as mock_update:
        import jira_handler
        jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_get.assert_not_called()  # the whole point of the cache
    mock_update.assert_called_once()
    fields = mock_update.call_args[0][4]
    assert fields["Title"] == "New title"


def test_cache_miss_falls_back_to_get_record_and_populates():
    """Empty cache → get_record runs (HTTP), result lands in cache for next call."""
    assert "recNEW" not in lark_api._record_cache

    with patch("lark_api._request",
               return_value=_fake_get_record_response("recNEW", {"Title": "from-lark"})):
        result = lark_api.get_cached_or_fetch_record("tok", "base", "tbl", "recNEW")

    assert result == {"record_id": "recNEW", "fields": {"Title": "from-lark"}}
    assert "recNEW" in lark_api._record_cache
    assert lark_api._record_cache["recNEW"]["fields"] == {"Title": "from-lark"}


def test_ttl_expiry_refetches_from_lark():
    """Stale cache entry (expires_at in past) → falls back to get_record."""
    lark_api._record_cache["recOLD"] = {
        "fields": {"Title": "stale"},
        "expires_at": time.time() - 1,  # already expired
    }

    with patch("lark_api._request",
               return_value=_fake_get_record_response("recOLD", {"Title": "fresh"})) as mock_req:
        result = lark_api.get_cached_or_fetch_record("tok", "base", "tbl", "recOLD")

    mock_req.assert_called_once()  # HTTP call HAPPENED — cache was treated as miss
    assert result["fields"] == {"Title": "fresh"}
    assert lark_api._record_cache["recOLD"]["fields"] == {"Title": "fresh"}


def test_loop_guard_preserved_when_cached_value_matches_changelog():
    """VR-272 regression: cache holds the value Jira changelog also claims is
    new → value-compare sees match → no write → no ping-pong loop possible.

    This is the same safeguard that exists when fetching via get_record;
    moving the read through the cache must not weaken it.
    """
    index._jira_to_lark["PROJ-1"] = "recABC"
    index._lark_to_jira["recABC"] = "PROJ-1"
    lark_api._record_cache["recABC"] = {
        "fields": {"Title": "Same"},
        "expires_at": time.time() + 60,
    }

    changelog = {"items": [{"field": "summary", "toString": "Same", "to": None}]}
    with patch("lark_api.get_token", return_value="tok"), \
         patch("lark_api.get_record") as mock_get, \
         patch("lark_api.update_record") as mock_update:
        import jira_handler
        jira_handler.process("jira:issue_updated", ISSUE, changelog, CFG)

    mock_get.assert_not_called()
    mock_update.assert_not_called()  # no write → no loop seed


def _fake_update_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"code": 0, "data": {}}
    return resp


def test_update_record_merges_written_fields_into_cache():
    """A write to Lark immediately refreshes the cache so the next read sees
    the new value without a get_record fetch."""
    lark_api._record_cache["recABC"] = {
        "fields": {"Title": "old", "R. MD": 3},
        "expires_at": time.time() + 60,
    }

    with patch("lark_api._request", return_value=_fake_update_response()):
        lark_api.update_record("tok", "base", "tbl", "recABC", {"Title": "new"})

    assert lark_api._record_cache["recABC"]["fields"]["Title"] == "new"
    # Merge: untouched field still present
    assert lark_api._record_cache["recABC"]["fields"]["R. MD"] == 3


def test_update_record_drops_entry_when_uncacheable_key_written():
    """Writing a registered uncacheable key (e.g. Lark link fields whose
    write-shape doesn't round-trip with get_record's read-shape) invalidates
    the entire cache entry — safer than storing wrong-shape data."""
    lark_api._uncacheable_write_keys.add("Parent items")
    try:
        lark_api._record_cache["recABC"] = {
            "fields": {"Title": "t", "Parent items": [{"record_ids": ["recOldParent"]}]},
            "expires_at": time.time() + 60,
        }

        with patch("lark_api._request", return_value=_fake_update_response()):
            lark_api.update_record("tok", "base", "tbl", "recABC",
                                   {"Parent items": ["recNewParent"]})

        # Entry dropped entirely — next read will refetch with correct shape
        assert "recABC" not in lark_api._record_cache
    finally:
        lark_api._uncacheable_write_keys.discard("Parent items")


def test_lark_webhook_decode_merges_into_cache():
    """A Lark record_edited webhook's decoded fields land in cache for that
    record_id, so a subsequent Jira webhook on the same record sees them
    without a get_record fetch (the populate point that closes the read
    loop for bidirectional sync)."""
    lark_api._record_cache["recABC"] = {
        "fields": {"Title": "old", "Assignee": ["Tawan"]},
        "expires_at": time.time() + 60,
    }
    after_value = [{"field_id": "fldTitle", "field_value": "fresh"}]
    before_value = [{"field_id": "fldTitle", "field_value": "old"}]
    fake_meta = {"fldTitle": {"name": "Title", "type": 1, "options": {}}}

    with patch("lark_api.get_field_meta_by_id", return_value=fake_meta):
        import lark_handler
        decoded = lark_handler._decode_after_value(
            after_value, "tok", CFG,
            before_value=before_value, record_id="recABC")

    assert decoded == {"Title": "fresh"}
    assert lark_api._record_cache["recABC"]["fields"]["Title"] == "fresh"
    # Untouched fields preserved
    assert lark_api._record_cache["recABC"]["fields"]["Assignee"] == ["Tawan"]


def test_fetch_all_records_overwrites_cache_for_drift_repair():
    """A reconcile-loop fetch_all_records call is the canonical drift repair:
    it must overwrite cached entries with the live Lark state so divergence
    (e.g. someone edited Lark outside our pipeline) is corrected before the
    next sync runs comparisons against cached values."""
    lark_api._record_cache["recABC"] = {
        "fields": {"Title": "stale-in-cache"},
        "expires_at": time.time() + 60,
    }

    def fake_request(method, url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {"record_id": "recABC", "fields": {"Title": "real-on-lark"}},
                    {"record_id": "recXYZ", "fields": {"Title": "another"}},
                ],
                "has_more": False,
            },
        }
        return resp

    with patch("lark_api._request", side_effect=fake_request):
        records = lark_api.fetch_all_records("tok", "base", "tbl")

    assert len(records) == 2
    # Cache reflects live Lark state — divergence repaired
    assert lark_api._record_cache["recABC"]["fields"]["Title"] == "real-on-lark"
    # New record also populated
    assert lark_api._record_cache["recXYZ"]["fields"]["Title"] == "another"


def test_kill_switch_bypasses_cache_reads():
    """Layer-1 rollback: when lark_value_cache_enabled is False, cache reads
    are bypassed and get_cached_or_fetch_record goes straight to get_record.
    Flipping the switch must not require a redeploy."""
    lark_api._record_cache["recABC"] = {
        "fields": {"Title": "cached"},
        "expires_at": time.time() + 60,
    }
    lark_api.set_value_cache_enabled(False)
    try:
        with patch("lark_api._request",
                   return_value=_fake_get_record_response("recABC", {"Title": "fresh"})) as mock_req:
            result = lark_api.get_cached_or_fetch_record("tok", "base", "tbl", "recABC")

        mock_req.assert_called_once()  # HTTP HIT despite cached entry
        assert result["fields"]["Title"] == "fresh"
    finally:
        lark_api.set_value_cache_enabled(True)


def test_invalidate_record_cache_clears_all():
    """Table switch must invalidate the entire cache — cached fields belong
    to the previous table, mixing them with the new table is a data hazard."""
    lark_api._record_cache["recA"] = {"fields": {"Title": "a"}, "expires_at": time.time() + 60}
    lark_api._record_cache["recB"] = {"fields": {"Title": "b"}, "expires_at": time.time() + 60}

    lark_api.invalidate_record_cache()

    assert lark_api._record_cache == {}


def test_delete_record_invalidates_entry():
    """Deleting a record drops its cache entry — a future read for that
    record_id must not return a 'ghost' value from the deleted record."""
    lark_api._record_cache["recGONE"] = {
        "fields": {"Title": "doomed"},
        "expires_at": time.time() + 60,
    }
    with patch("lark_api._request", return_value=_fake_update_response()):
        lark_api.delete_record("tok", "base", "tbl", "recGONE")

    assert "recGONE" not in lark_api._record_cache
