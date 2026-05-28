"""Tests for per-call-type + retry instrumentation on the Lark API counter.

Purpose: the global counter can't tell us WHICH Lark calls dominate the
monthly quota, and it under-reports by ~20% because retries aren't counted.
These tests pin down a breakdown by call type (counting every HTTP attempt,
including retries) so /debug/lark-calls reconciles with the Lark admin figure.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from unittest.mock import patch, MagicMock
import lark_api

BASE = lark_api.LARK_BASE_URL


def setup_function():
    lark_api._call_counts.clear()
    lark_api._calls_by_type.clear()
    lark_api._retries_by_type.clear()


def _resp(status):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    return r


def test_classify_maps_each_lark_url_pattern():
    c = lark_api._classify
    assert c("POST", f"{BASE}/auth/v3/app_access_token/internal") == "get_token"
    assert c("GET", f"{BASE}/bitable/v1/apps/B/tables/T/records") == "fetch_all_records"
    assert c("GET", f"{BASE}/bitable/v1/apps/B/tables/T/records/rec123") == "get_record"
    assert c("POST", f"{BASE}/bitable/v1/apps/B/tables/T/records/search") == "search_records"
    assert c("POST", f"{BASE}/bitable/v1/apps/B/tables/T/records") == "create_record"
    assert c("PUT", f"{BASE}/bitable/v1/apps/B/tables/T/records/rec123") == "update_record"
    assert c("DELETE", f"{BASE}/bitable/v1/apps/B/tables/T/records/rec123") == "delete_record"
    assert c("GET", f"{BASE}/bitable/v1/apps/B/tables/T/fields") == "list_fields"
    assert c("GET", f"{BASE}/bitable/v1/apps/B/tables") == "list_tables"


def test_request_counts_attempt_by_type():
    """A single successful call increments the per-type attempt counter once."""
    with patch("lark_api.requests.request", return_value=_resp(200)):
        lark_api._request("GET", f"{BASE}/bitable/v1/apps/B/tables/T/records/rec1")
    assert lark_api._calls_by_type["get_record"] == 1
    assert lark_api._retries_by_type["get_record"] == 0


def test_request_counts_retries_by_type():
    """A 429 then 200 = 2 HTTP attempts, 1 of them a retry — both tracked."""
    seq = [_resp(429), _resp(200)]
    with patch("lark_api.requests.request", side_effect=seq), \
         patch("lark_api.time.sleep"):
        lark_api._request("PUT", f"{BASE}/bitable/v1/apps/B/tables/T/records/rec1")
    assert lark_api._calls_by_type["update_record"] == 2   # actual HTTP attempts
    assert lark_api._retries_by_type["update_record"] == 1  # the retry


def test_call_stats_exposes_breakdown_and_retry_total():
    with patch("lark_api.requests.request", side_effect=[_resp(429), _resp(200)]), \
         patch("lark_api.time.sleep"):
        lark_api._request("GET", f"{BASE}/bitable/v1/apps/B/tables/T/records/rec1")
    with patch("lark_api.requests.request", return_value=_resp(200)):
        lark_api._request("POST", f"{BASE}/bitable/v1/apps/B/tables/T/records")

    stats = lark_api.call_stats()
    assert stats["by_type"]["get_record"] == 2
    assert stats["by_type"]["create_record"] == 1
    assert stats["retries_total"] == 1
    assert stats["retries_by_type"]["get_record"] == 1
