"""Env var loading, field constants, assignee maps."""
import os
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


def set_active_table(table_id: str, table_name: str = "") -> None:
    global _active_table_id, _active_table_name
    _active_table_id   = table_id
    _active_table_name = table_name


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
