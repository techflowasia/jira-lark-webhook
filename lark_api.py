"""Lark OpenAPI client."""
import logging
import random
import time
import requests

log = logging.getLogger(__name__)

LARK_BASE_URL = "https://open.larksuite.com/open-apis"
_token_cache = {"token": None, "expires_at": 0}

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
        for item in data["data"]["items"]:
            records.append({"record_id": item["record_id"], "fields": item.get("fields", {})})
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
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
    return {"record_id": item["record_id"], "fields": item.get("fields", {})}


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


def delete_record(token: str, base_token: str, table_id: str, record_id: str) -> None:
    resp = _request("DELETE",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark delete error: {data.get('msg')}")


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


def list_fields(token: str, base_token: str, table_id: str) -> list:
    """Return [{field_name, field_id, type}, ...] for the active table."""
    resp = _request("GET",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
        headers=_headers(token), params={"page_size": 300})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark list_fields error: {data.get('msg')}")
    return [{"field_name": f["field_name"], "field_id": f["field_id"]}
            for f in data.get("data", {}).get("items", [])]


def get_select_options(token: str, base_token: str, table_id: str, field_name: str) -> list:
    """Return option names for a select field."""
    resp = _request("GET",
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
        headers=_headers(token), params={"page_size": 300})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark get_select_options error: {data.get('msg')}")
    for f in data.get("data", {}).get("items", []):
        if f["field_name"] == field_name:
            options = (f.get("property") or {}).get("options", [])
            return [opt["name"] for opt in options]
    return []
