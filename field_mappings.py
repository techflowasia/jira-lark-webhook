"""Field mapping cache — loads from Supabase, falls back to hardcoded defaults."""
import logging
import history

log = logging.getLogger(__name__)

_cache: list = []

# Hardcoded fallback (matches Supabase seed data)
_DEFAULTS = [
    {"id": 0, "lark_field": "Title",             "jira_field": "summary",           "jira_label": "Title",        "direction": "both",         "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Timeline - Start",  "jira_field": "customfield_10015", "jira_label": "Start Date",   "direction": "both",         "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Timeline - End",    "jira_field": "duedate",           "jira_label": "Due Date",     "direction": "both",         "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Assignee",          "jira_field": "assignee",          "jira_label": "Assignee",     "direction": "both",         "field_type": "user",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Type",              "jira_field": "issuetype",         "jira_label": "Issue Type",   "direction": "both",         "field_type": "select", "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Parent items",      "jira_field": "parent",            "jira_label": "Parent",       "direction": "both",         "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Jira Key",          "jira_field": "key",               "jira_label": "Jira Key",     "direction": "jira_to_lark", "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Jira URL",          "jira_field": "url",               "jira_label": "Jira URL",     "direction": "jira_to_lark", "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "R. MD",             "jira_field": "customfield_10016", "jira_label": "Story Points", "direction": "jira_to_lark", "field_type": "number", "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Release",           "jira_field": "fixVersions",       "jira_label": "Fix Version",  "direction": "lark_to_jira", "field_type": "select", "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Actual start date", "jira_field": "customfield_10175", "jira_label": "Actual Start", "direction": "jira_to_lark", "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Actual end date",   "jira_field": "customfield_10176", "jira_label": "Actual End",   "direction": "jira_to_lark", "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Jira status",       "jira_field": "status",            "jira_label": "Jira Status",  "direction": "jira_to_lark", "field_type": "select", "is_system": True,  "active": True},
]


def load() -> None:
    global _cache
    client = history._get_client()
    if not client:
        _cache = list(_DEFAULTS)
        return
    try:
        rows = client.table("field_mappings").select("*").order("id").execute()
        _cache = rows.data or list(_DEFAULTS)
    except Exception as e:
        log.warning(f"field_mappings.load failed: {e}")
        _cache = list(_DEFAULTS)


def get_all() -> list:
    return list(_cache)


def get_custom_lark_to_jira() -> list:
    """Non-system active mappings that sync Lark → Jira."""
    return [m for m in _cache
            if not m.get("is_system") and m.get("active")
            and m.get("direction") in ("both", "lark_to_jira")]


def get_custom_jira_to_lark() -> list:
    """Non-system active mappings that sync Jira → Lark."""
    return [m for m in _cache
            if not m.get("is_system") and m.get("active")
            and m.get("direction") in ("both", "jira_to_lark")]


def upsert(row: dict) -> dict:
    """Insert or update a field mapping. Returns the saved row."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    data = {k: row[k] for k in ("lark_field", "jira_field", "jira_label",
                                  "direction", "field_type", "is_system", "active")
            if k in row}
    if row.get("id"):
        res = client.table("field_mappings").update(data).eq("id", row["id"]).execute()
    else:
        res = client.table("field_mappings").insert(data).execute()
    load()
    return (res.data or [{}])[0]


def delete(mapping_id: int) -> None:
    """Delete a non-system mapping."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    client.table("field_mappings").delete().eq("id", mapping_id).eq("is_system", False).execute()
    load()


def update_lark_field(mapping_id: int, new_lark_field: str) -> None:
    """Update only the lark_field name of a system mapping."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    client.table("field_mappings").update({"lark_field": new_lark_field}).eq("id", mapping_id).execute()
    load()
