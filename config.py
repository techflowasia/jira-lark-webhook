"""Env var loading, field constants, assignee maps."""
import os
import json
from datetime import timezone, timedelta
from urllib.parse import urlparse
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

def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _env_json(name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} is not valid JSON: {e}") from e


# Workspace profile. Every default below is the value this service was
# originally hardcoded to, so a deployment that sets none of these env vars
# behaves exactly as before.
JF_START         = _env("JIRA_FIELD_START_DATE",   "customfield_10015")
JF_STORY_POINTS  = _env("JIRA_FIELD_STORY_POINTS", "customfield_10016")
JF_ACTUAL_START  = _env("JIRA_FIELD_ACTUAL_START", "customfield_10175")
JF_ACTUAL_END    = _env("JIRA_FIELD_ACTUAL_END",   "customfield_10176")
JF_SPRINT        = _env("JIRA_FIELD_SPRINT",       "customfield_10020")

LARK_API_BASE    = _env("LARK_API_BASE", "https://open.larksuite.com/open-apis").rstrip("/")
LARK_API_HOST    = urlparse(LARK_API_BASE).hostname or ""
LARK_BOT_OPEN_ID = _env("LARK_BOT_OPEN_ID", "ou_6c4fb657f0b844228990210a8fc789b5")
PUBLIC_URL       = _env("PUBLIC_URL", "https://jira-lark-webhook.onrender.com").rstrip("/")

TZ_OFFSET_HOURS  = float(_env("TZ_OFFSET_HOURS", "7"))
LOCAL_TZ         = timezone(timedelta(hours=TZ_OFFSET_HOURS))
_tz_minutes      = round(abs(TZ_OFFSET_HOURS) * 60)
TZ_LABEL         = ("-" if TZ_OFFSET_HOURS < 0 else "+") + f"{_tz_minutes // 60:02d}" \
                   + (f"{_tz_minutes % 60:02d}" if _tz_minutes % 60 else "")

DEFAULT_SYNC_TYPES = _env_json("DEFAULT_SYNC_TYPES", ["Epic", "Story", "Task"])

JIRA_TO_LARK_ASSIGNEE = _env_json("ASSIGNEE_MAP", {
    "Tawan Vongsombun":        "Tawan",
    "Thet Swe Lin":            "Lin",
    "Benyapha Kasemtanakitti": "Nurse",
    "Moe Pyae Pyae Kyaw":      "Iris",
    "Waritsara Matnok":        "Min",
})
LARK_TO_JIRA_ASSIGNEE = {v: k for k, v in JIRA_TO_LARK_ASSIGNEE.items()}


_active_table_id:   str = ""
_active_table_name: str = ""

_allowed_jira_types: set = set(DEFAULT_SYNC_TYPES)
_allowed_lark_types: set = set(DEFAULT_SYNC_TYPES)


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
