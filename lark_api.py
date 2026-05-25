"""Lark OpenAPI client."""
import logging
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import requests

log = logging.getLogger(__name__)

LARK_BASE_URL = "https://open.larksuite.com/open-apis"
_token_cache = {"token": None, "expires_at": 0}

# Per-day Lark API call counter (Bangkok day boundary, matching the rest of
# the project's date handling). In-memory only — resets on process restart;
# the Lark Developer Console is authoritative for the monthly quota. Exposed
# at GET /debug/lark-calls to watch the quota-reduction work land.
_BKK = timezone(timedelta(hours=7))
_call_counts: dict = defaultdict(int)
_call_counts_lock = threading.Lock()
_process_start = time.time()


def call_stats() -> dict:
    """Snapshot of Lark API calls made by this process."""
    today = datetime.now(_BKK).strftime("%Y-%m-%d")
    with _call_counts_lock:
        by_day = dict(_call_counts)
    month_prefix = today[:7]
    return {
        "today": by_day.get(today, 0),
        "this_month": sum(v for k, v in by_day.items() if k.startswith(month_prefix)),
        "total_since_start": sum(by_day.values()),
        "by_day": dict(sorted(by_day.items())),
        "process_started": datetime.fromtimestamp(_process_start, tz=_BKK)
                            .strftime("%Y-%m-%d %H:%M:%S +07"),
    }

# Short-lived cache for table field schemas. The dashboard hits /api/lark-fields
# on every page load AND on every "+ Add Field" click — schemas rarely change
# but Lark's bitable QPS cap (~20/s) trips a 429 that even retry/backoff can't
# clear, leaving the field dropdown stuck on "Loading…". TTL is short so users
# still see schema edits within a minute.
_fields_cache: dict = {}
_FIELDS_TTL = 60.0

# In-memory cache of Lark record values, populated as a free side effect of
# every read/write/webhook path. Eliminates the get_record call on the
# Jira→Lark update hot path (jira_handler._handle_update line 119) — the
# dominant remaining consumer of the Lark Basic 10k/month quota after the
# prior optimizations in CHANGELOG (599c342, 44ee5a3, 672e4b4).
# Entry shape: {record_id: {"fields": {<same as get_record returns>},
#                           "expires_at": float}}
_record_cache: dict = {}
_RECORD_CACHE_TTL = 300.0  # 5 min; conservative, bump after monitoring

# Field names whose write-shape doesn't round-trip with get_record's read-shape
# (e.g., Lark link fields are written as [rid_string] but returned as
# [{"record_ids": [rid_string], ...}]). Writing such a field invalidates the
# cache entry entirely instead of merging — next read refetches fresh.
# Callers register their field names at startup; lark_api stays decoupled
# from domain field constants in config.py.
_uncacheable_write_keys: set = set()

# Layer-1 rollback kill switch (per the rollback plan in the work's handoff).
# When False, get_cached_or_fetch_record skips the cache entry and goes
# straight to get_record — flipping this in Supabase does not require a
# redeploy. Populates still run on the side so a flip back to True comes
# online warm.
_value_cache_enabled: bool = True


def set_value_cache_enabled(enabled: bool) -> None:
    """Toggle the value cache. Read by get_cached_or_fetch_record."""
    global _value_cache_enabled
    _value_cache_enabled = bool(enabled)


def invalidate_record_cache(record_id: "str | None" = None) -> None:
    """Drop one record's cache entry, or the whole cache when record_id=None.

    Called on table switch (per-table cache contents are not portable) and
    on per-record deletes.
    """
    if record_id is None:
        _record_cache.clear()
    else:
        _record_cache.pop(record_id, None)

# Retry policy for transient Lark API failures (429 + 5xx). Active editing
# generates bursts of record_edited webhooks that each spawn a background
# handler hitting Lark — without backoff the bitable QPS cap (≈20/sec) trips
# 429s in a thundering herd.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_DELAY = 0.5  # seconds; doubles each attempt, capped


def _sleep_for(resp: requests.Response, attempt: int) -> float:
    """Prefer Retry-After when present; otherwise exponential backoff + jitter."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.1, float(retry_after))
        except ValueError:
            pass
    return min(_BASE_DELAY * (2 ** attempt), 8.0) + random.uniform(0, 0.25)


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """requests.request wrapper with retry/backoff on 429 + 5xx."""
    day = datetime.now(_BKK).strftime("%Y-%m-%d")
    with _call_counts_lock:
        _call_counts[day] += 1
    for attempt in range(_MAX_RETRIES):
        resp = requests.request(method, url, timeout=30, **kwargs)
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        if attempt == _MAX_RETRIES - 1:
            return resp
        delay = _sleep_for(resp, attempt)
        log.warning(
            "Lark %s %s -> %s; retrying in %.2fs (attempt %d/%d)",
            method, url, resp.status_code, delay, attempt + 1, _MAX_RETRIES,
        )
        time.sleep(delay)
    return resp


def get_token(app_id: str, app_secret: str) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = _request("POST", f"{LARK_BASE_URL}/auth/v3/app_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret})
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_all_records(token: str, base_token: str, table_id: str) -> list:
    records, page_token = [], None
    while True:
        params = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        resp = _request("GET",
            f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records",
            headers=_headers(token), params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark API error: {data.get('msg')}")
        now_expires = time.time() + _RECORD_CACHE_TTL
        for item in data["data"]["items"]:
            fields = item.get("fields", {})
            records.append({"record_id": item["record_id"], "fields": fields})
            # Bulk-populate the value cache. Reconcile uses this as its drift
            # repair: overwriting any stale entries with the live Lark state.
            _record_cache[item["record_id"]] = {
                "fields": fields, "expires_at": now_expires,
            }
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def find_modified_time_field(token: str, base_token: str, table_id: str) -> "str | None":
    """Return the name of the table's 'Last modified time' system field, or None.

    Reuses the 60 s field-schema cache (no extra Lark call). Detected by
    ui_type 'ModifiedTime' so it works regardless of the user-chosen field
    name or workspace language. None means the incremental reconcile path
    is unavailable → callers fall back to a full fetch.
    """
    for f in _fetch_field_items(token, base_token, table_id):
        # Lark represents "Last modified time" as ui_type ModifiedTime /
        # numeric type 1002. Check both so detection survives API
        # representation differences and workspace language.
        if (f.get("ui_type") or "") == "ModifiedTime" or f.get("type") == 1002:
            return f.get("field_name")
    return None


def search_records_modified_since(token: str, base_token: str, table_id: str,
                                  modified_field_name: str, since_ts_ms: int) -> list:
    """Records whose last-modified time is at/after since_ts_ms.

    Uses the bitable records/search endpoint. Lark date filters are
    day-granular (the timestamp is floored to midnight in the doc timezone),
    which only over-fetches slightly — acceptable for a safety-net reconcile.
    """
    records, page_token = [], None
    body = {
        "filter": {
            "conjunction": "and",
            "conditions": [{
                "field_name": modified_field_name,
                "operator": "isGreater",
                "value": ["ExactDate", str(int(since_ts_ms))],
            }],
        },
    }
    while True:
        params = {"page_size": 200}
        if page_token:
            params["page_token"] = page_token
        resp = _request("POST",
            f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/search",
            headers=_headers(token), params=params, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark search error: {data.get('msg')}")
        now_expires = time.time() + _RECORD_CACHE_TTL
        for item in data.get("data", {}).get("items", []):
            fields = item.get("fields", {})
            records.append({"record_id": item["record_id"], "fields": fields})
            _record_cache[item["record_id"]] = {
                "fields": fields, "expires_at": now_expires,
            }
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")
    return records


def get_record(token: str, base_token: str, table_id: str, record_id: str) -> dict:
    resp = _request("GET",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark get_record error: {data.get('msg')}")
    item = data["data"]["record"]
    fields = item.get("fields", {})
    _record_cache[item["record_id"]] = {
        "fields": fields, "expires_at": time.time() + _RECORD_CACHE_TTL,
    }
    return {"record_id": item["record_id"], "fields": fields}


def _cache_merge(record_id: str, fields: dict) -> None:
    """Merge read-shape fields into an existing cache entry (TTL refreshed).

    No-op if no entry exists — we never seed a new entry from a partial
    update, because partial data would mislead value-compares for the
    fields we don't have. Assumes `fields` are already in get_record's
    read-shape (e.g. from a webhook decode, or from get_record itself).
    """
    entry = _record_cache.get(record_id)
    if entry is not None:
        entry["fields"].update(fields)
        entry["expires_at"] = time.time() + _RECORD_CACHE_TTL


def get_cached_or_fetch_record(token: str, base_token: str, table_id: str,
                               record_id: str) -> dict:
    """Return the Lark record dict, preferring the in-memory cache.

    On a fresh cache hit, returns without making any Lark API call. On miss
    or stale entry, falls back to get_record. Output shape matches get_record:
    {"record_id": ..., "fields": {...}}.
    """
    if _value_cache_enabled:
        entry = _record_cache.get(record_id)
        if entry is not None and time.time() < entry["expires_at"]:
            return {"record_id": record_id, "fields": entry["fields"]}
    return get_record(token, base_token, table_id, record_id)


def create_record(token: str, base_token: str, table_id: str, fields: dict) -> str:
    resp = _request("POST",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records",
        headers=_headers(token), json={"fields": fields})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark create error: {data.get('msg')}")
    return data["data"]["record"]["record_id"]


def update_record(token: str, base_token: str, table_id: str,
                  record_id: str, fields: dict) -> None:
    resp = _request("PUT",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token), json={"fields": fields})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark update error: {data.get('msg')}")
    # Merge written fields into existing cache entry so the next read sees
    # them without a get_record fetch. We do NOT seed a new entry from a
    # write alone — partial data would mislead value-compares for fields
    # we didn't write. Some field types (e.g. Lark link fields, written as
    # [rid_string] but returned by get_record as [{"record_ids":[...]}])
    # don't round-trip cleanly; those keys are registered in
    # _uncacheable_write_keys and invalidate the entire entry instead.
    entry = _record_cache.get(record_id)
    if entry is not None:
        if any(k in _uncacheable_write_keys for k in fields):
            _record_cache.pop(record_id, None)
        else:
            entry["fields"].update(fields)
            entry["expires_at"] = time.time() + _RECORD_CACHE_TTL


def delete_record(token: str, base_token: str, table_id: str, record_id: str) -> None:
    resp = _request("DELETE",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark delete error: {data.get('msg')}")
    # Drop the cache entry so a future read for this record_id can't return a
    # ghost value from the deleted record.
    _record_cache.pop(record_id, None)


def list_tables(token: str, base_token: str) -> list:
    """Return [{table_id, name}, ...] for all tables in the Base."""
    resp = _request("GET",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables",
        headers=_headers(token), params={"page_size": 100})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark list_tables error: {data.get('msg')}")
    return [{"table_id": t["table_id"], "name": t["name"]}
            for t in data.get("data", {}).get("items", [])]


def _fetch_field_items(token: str, base_token: str, table_id: str) -> list:
    """Fetch raw field items for a table, with a short TTL cache."""
    key = (base_token, table_id)
    entry = _fields_cache.get(key)
    now = time.time()
    if entry and now < entry["expires_at"]:
        return entry["items"]
    resp = _request("GET",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
        headers=_headers(token), params={"page_size": 300})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark fields error: {data.get('msg')}")
    items = data.get("data", {}).get("items", [])
    _fields_cache[key] = {"items": items, "expires_at": now + _FIELDS_TTL}
    return items


def invalidate_fields_cache(base_token=None, table_id=None) -> None:
    """Drop cached field schemas. Call after switching active table."""
    if base_token is None or table_id is None:
        _fields_cache.clear()
    else:
        _fields_cache.pop((base_token, table_id), None)


def list_fields(token: str, base_token: str, table_id: str) -> list:
    """Return [{field_name, field_id}, ...] for the active table."""
    items = _fetch_field_items(token, base_token, table_id)
    return [{"field_name": f["field_name"], "field_id": f["field_id"]} for f in items]


def get_select_options(token: str, base_token: str, table_id: str, field_name: str) -> list:
    """Return option names for a select field."""
    items = _fetch_field_items(token, base_token, table_id)
    for f in items:
        if f["field_name"] == field_name:
            options = (f.get("property") or {}).get("options", [])
            return [opt["name"] for opt in options]
    return []


def get_field_meta_by_id(token: str, base_token: str, table_id: str) -> dict:
    """Map field_id -> {name, type, options} for decoding webhook payloads.

    `options` is {option_id: option_name} for single/multi-select fields,
    empty otherwise. Reuses the 60 s _fetch_field_items cache so this adds
    no extra Lark API calls on the webhook hot path.
    """
    items = _fetch_field_items(token, base_token, table_id)
    meta = {}
    for f in items:
        fid = f.get("field_id")
        if not fid:
            continue
        options = (f.get("property") or {}).get("options") or []
        meta[fid] = {
            "name": f.get("field_name"),
            "type": f.get("type"),
            "options": {o["id"]: o["name"] for o in options if o.get("id")},
        }
    return meta
