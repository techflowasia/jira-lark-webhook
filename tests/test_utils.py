"""Tests for utils helpers."""
import sys, os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils import _lark_link_rid, _jira_date_to_lark_ts, _lark_ts_to_jira_date


def test_lark_link_rid_v1_record_ids_plural():
    """Current Lark v1 Bitable shape — record_ids is a list."""
    value = [{"record_ids": ["rec1"], "text": "x", "type": "text"}]
    assert _lark_link_rid(value) == "rec1"


def test_lark_link_rid_legacy_record_id_singular():
    assert _lark_link_rid([{"record_id": "rec1"}]) == "rec1"


def test_lark_link_rid_legacy_id():
    assert _lark_link_rid([{"id": "rec1"}]) == "rec1"


def test_lark_link_rid_empty_list():
    assert _lark_link_rid([]) is None


def test_lark_link_rid_none():
    assert _lark_link_rid(None) is None


def test_lark_link_rid_multiple_links_returns_first():
    value = [
        {"record_ids": ["recA"], "text": "A"},
        {"record_ids": ["recB"], "text": "B"},
    ]
    assert _lark_link_rid(value) == "recA"


def test_lark_link_rid_skips_non_dict_items():
    value = ["junk", None, {"record_ids": ["recC"]}]
    assert _lark_link_rid(value) == "recC"


def test_lark_link_rid_accepts_single_dict_not_in_list():
    assert _lark_link_rid({"record_ids": ["rec1"]}) == "rec1"


# ---- Regression: 2026-05 one-day date-drift + runaway rewrite incident ----
# Root cause: _jira_date_to_lark_ts used Bangkok midnight (17:00Z the prior
# day); Lark Bitable Date fields are UTC-midnight, so every Jira↔Lark date
# round-trip lost a day and the Bangkok-vs-UTC value-compare never converged
# (endless reconcile/webhook rewrites). These tests fail on the old _BKK code.

def test_jira_date_to_lark_ts_is_exactly_utc_midnight():
    """The ts MUST be UTC 00:00:00 of the same calendar day — this is what
    Lark stores, so the loop-guard value-compare converges (no rewrites)."""
    ts = _jira_date_to_lark_ts("2026-06-14")
    expected = int(datetime(2026, 6, 14, tzinfo=timezone.utc).timestamp() * 1000)
    assert ts == expected
    # Old _BKK code produced 2026-06-13T17:00:00Z (the bug); guard against it.
    assert ts != int(datetime(2026, 6, 13, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_jira_lark_date_round_trip_preserves_day():
    """Jira date → Lark ts → Jira date must be the SAME day, every day of a
    DST-free, offset-sensitive span. A one-day loss here is the incident."""
    for d in ("2026-05-26", "2026-05-31", "2026-06-01", "2026-06-14",
              "2026-06-15", "2026-12-31", "2027-01-01"):
        assert _lark_ts_to_jira_date(_jira_date_to_lark_ts(d)) == d


def test_lark_user_set_utc_midnight_reads_back_same_day():
    """A date a user picks in Lark (stored UTC-midnight ms) → Jira date must
    be that same day, not one day earlier (the Lark→Jira leg of the bug)."""
    user_ms = int(datetime(2026, 6, 14, tzinfo=timezone.utc).timestamp() * 1000)
    assert _lark_ts_to_jira_date(user_ms) == "2026-06-14"


def _lark_date_field_truncate(ts_ms):
    """Faithful model of how a Lark Bitable Date field normalizes an incoming
    ms value: time-of-day is dropped to UTC midnight of that instant's UTC
    day. This truncation, combined with the old Bangkok-midnight write, is
    what silently lost a day on every Jira↔Lark round-trip."""
    return (int(ts_ms) // 86_400_000) * 86_400_000


def test_full_jira_lark_jira_round_trip_through_lark_truncation():
    """The actual incident path: Jira date → write to Lark → Lark truncates
    to UTC-day → read back → write to Jira. Must be the SAME day and STABLE
    (a second pass changes nothing — no runaway rewrite). Fails on _BKK."""
    for d in ("2026-05-26", "2026-05-30", "2026-05-31", "2026-06-01",
              "2026-06-13", "2026-06-14", "2026-06-15"):
        stored = _lark_date_field_truncate(_jira_date_to_lark_ts(d))
        assert _lark_ts_to_jira_date(stored) == d, f"day lost for {d}"
        # Loop-guard: re-deriving the ts from the round-tripped Jira value
        # must equal what Lark already holds, so the value-compare is a
        # no-op and reconcile/webhook handlers stop rewriting.
        assert _jira_date_to_lark_ts(_lark_ts_to_jira_date(stored)) == stored
