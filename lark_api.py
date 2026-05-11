"""Lark OpenAPI client."""
import time
import requests

LARK_BASE_URL = "https://open.larksuite.com/open-apis"
_token_cache = {"token": None, "expires_at": 0}


def get_token(app_id: str, app_secret: str) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = requests.post(f"{LARK_BASE_URL}/auth/v3/app_access_token/internal",
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
        resp = requests.get(
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
    resp = requests.get(
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark get_record error: {data.get('msg')}")
    item = data["data"]["record"]
    return {"record_id": item["record_id"], "fields": item.get("fields", {})}


def create_record(token: str, base_token: str, table_id: str, fields: dict) -> str:
    resp = requests.post(
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records",
        headers=_headers(token), json={"fields": fields})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark create error: {data.get('msg')}")
    return data["data"]["record"]["record_id"]


def update_record(token: str, base_token: str, table_id: str,
                  record_id: str, fields: dict) -> None:
    resp = requests.put(
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token), json={"fields": fields})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark update error: {data.get('msg')}")


def delete_record(token: str, base_token: str, table_id: str, record_id: str) -> None:
    resp = requests.delete(
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
        headers=_headers(token))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark delete error: {data.get('msg')}")


def list_tables(token: str, base_token: str) -> list:
    """Return [{table_id, name}, ...] for all tables in the Base."""
    resp = requests.get(
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
    resp = requests.get(
        f"{LARK_BASE_URL}/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
        headers=_headers(token), params={"page_size": 300})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark list_fields error: {data.get('msg')}")
    return [{"field_name": f["field_name"], "field_id": f["field_id"]}
            for f in data.get("data", {}).get("items", [])]
