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
# Seed for project #1 only (projects._env_project / bootstrap). At runtime use
# jira_to_lark_assignee() / lark_to_jira_assignee(), which read the current project.
LARK_TO_JIRA_ASSIGNEE = {v: k for k, v in JIRA_TO_LARK_ASSIGNEE.items()}


def set_active_table(table_id: str, table_name: str = "") -> None:
    """Point the CURRENT project at a different Lark table (persisted when
    Supabase is available, in-memory otherwise)."""
    import projects
    projects.update(projects.current_id(),
                    {"lark_table_id": table_id, "lark_table_name": table_name})


def get_allowed_jira_types() -> set:
    import projects
    return set(projects.current().allowed_jira_types)


def get_allowed_lark_types() -> set:
    import projects
    return set(projects.current().allowed_lark_types)


def save_sync_types(jira_types: list, lark_types: list) -> None:
    """Save the CURRENT project's allowed sync types."""
    import projects
    projects.update(projects.current_id(), {"allowed_jira_types": list(jira_types),
                                            "allowed_lark_types": list(lark_types)})


def jira_to_lark_assignee() -> dict:
    """The CURRENT project's Jira display name -> Lark name map."""
    import projects
    return dict(projects.current().assignee_map)


def lark_to_jira_assignee() -> dict:
    return {v: k for k, v in jira_to_lark_assignee().items()}


def lark_assignee_for(display_name: "str | None") -> "tuple[str | None, bool]":
    """(lark_name_or_None, is_unmapped) for a Jira assignee display name in
    the CURRENT project. Unassigned -> (None, False); a person absent from the
    project's people list -> (None, True)."""
    if not display_name:
        return None, False
    lark_name = jira_to_lark_assignee().get(display_name)
    return lark_name, lark_name is None


def get_cfg() -> dict:
    import projects
    return projects.cfg_for(projects.current())
