"""Env var loading, field constants, assignee maps."""
import os
import json
from dotenv import load_dotenv

load_dotenv()

F_TITLE          = "Title"
F_START          = "Timeline - Start"
F_END            = "Timeline - End"
F_ASSIGNEE       = "Assignee"
F_JIRA_KEY       = "Jira Key"
F_JIRA_URL       = "Jira URL"
F_TYPE           = "Type"
F_PARENT         = "Parent items"
F_MD             = "R. MD"
F_RELEASE        = "Release"
F_ACTUAL_START   = "Actual start date"
F_ACTUAL_END     = "Actual end date"
F_JIRA_STATUS    = "Jira status"
F_STATUS         = "Status"

JIRA_TO_LARK_ASSIGNEE = {
    "Tawan Vongsombun":        "Tawan",
    "Thet Swe Lin":            "Lin",
    "Benyapha Kasemtanakitti": "Nurse",
    "Moe Pyae Pyae Kyaw":      "Iris",
    "Waritsara Matnok":        "Min",
}
LARK_TO_JIRA_ASSIGNEE = {v: k for k, v in JIRA_TO_LARK_ASSIGNEE.items()}


_active_table_id:   str = ""
_active_table_name: str = ""

_allowed_jira_types: set = {"Epic", "Story", "Task"}
_allowed_lark_types: set = {"Epic", "Story", "Task"}


def set_active_table(table_id: str, table_name: str = "") -> None:
    global _active_table_id, _active_table_name
    _active_table_id   = table_id
    _active_table_name = table_name


def get_allowed_jira_types() -> set:
    return set(_allowed_jira_types)


def get_allowed_lark_types() -> set:
    return set(_allowed_lark_types)


def load_sync_types() -> None:
    global _allowed_jira_types, _allowed_lark_types
    try:
        import history
        client = history._get_client()
        if not client:
            return
        rows = client.table("settings").select("*").in_(
            "key", ["allowed_jira_types", "allowed_lark_types"]).execute()
        for row in (rows.data or []):
            if row["key"] == "allowed_jira_types":
                _allowed_jira_types = set(json.loads(row["value"]))
            elif row["key"] == "allowed_lark_types":
                _allowed_lark_types = set(json.loads(row["value"]))
    except Exception:
        pass  # keep defaults on any error


def save_sync_types(jira_types: list, lark_types: list) -> None:
    global _allowed_jira_types, _allowed_lark_types
    import history
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    client.table("settings").upsert(
        {"key": "allowed_jira_types", "value": json.dumps(jira_types)}).execute()
    client.table("settings").upsert(
        {"key": "allowed_lark_types", "value": json.dumps(lark_types)}).execute()
    _allowed_jira_types = set(jira_types)
    _allowed_lark_types = set(lark_types)


def get_cfg() -> dict:
    return {
        "JIRA_EMAIL":      os.environ["JIRA_EMAIL"],
        "JIRA_TOKEN":      os.environ["JIRA_TOKEN"],
        "JIRA_DOMAIN":     os.environ["JIRA_DOMAIN"],
        "JIRA_PROJECT":    os.environ["JIRA_PROJECT"],
        "LARK_APP_ID":     os.environ["LARK_APP_ID"],
        "LARK_APP_SECRET": os.environ["LARK_APP_SECRET"],
        "LARK_BASE_TOKEN": os.environ["LARK_BASE_TOKEN"],
        "LARK_TABLE_ID":   _active_table_id or os.environ["LARK_TABLE_ID"],
    }
