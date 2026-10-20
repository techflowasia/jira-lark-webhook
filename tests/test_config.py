"""The workspace profile in config.py: defaults reproduce the originally
hardcoded workspace, and each env var actually reaches the code that uses it."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROFILE_VARS = [
    "JIRA_FIELD_START_DATE", "JIRA_FIELD_STORY_POINTS", "JIRA_FIELD_ACTUAL_START",
    "JIRA_FIELD_ACTUAL_END", "JIRA_FIELD_SPRINT", "LARK_API_BASE", "LARK_BOT_OPEN_ID",
    "PUBLIC_URL", "TZ_OFFSET_HOURS", "DEFAULT_SYNC_TYPES", "ASSIGNEE_MAP",
]

_PROBE = """
import json, unittest.mock as m
import config, field_mappings, history, jira_api, jira_handler, lark_api, lark_handler
field_mappings.load()
with m.patch("jira_api.requests") as req:
    req.post.return_value.json.return_value = {"issues": [], "isLast": True}
    jira_api.fetch_all_issues({"JIRA_EMAIL": "e", "JIRA_TOKEN": "t",
                               "JIRA_DOMAIN": "d", "JIRA_PROJECT": "P"})
    search = req.post.call_args.kwargs["json"]
history.record(direction="system", event="config", description="probe")
print(json.dumps({
    "search_fields": search["fields"],
    "jql": search["jql"],
    "changelog_fields": sorted(jira_handler.RELEVANT_CHANGELOG_FIELDS),
    "start_mapping": [d["jira_field"] for d in field_mappings.get_all()
                      if d["lark_field"] == "Timeline - Start"],
    "f_start": field_mappings.F_START,
    "lark_base_url": lark_api.LARK_BASE_URL,
    "lark_host": lark_handler.LARK_API_HOST,
    "bot": config.LARK_BOT_OPEN_ID,
    "public_url": config.PUBLIC_URL,
    "history_ts_suffix": history.recent(1)[0]["ts"].split(" ")[-1],
    "allowed_types": sorted(config.get_allowed_jira_types()),
    "to_lark": config.JIRA_TO_LARK_ASSIGNEE,
    "to_jira": config.LARK_TO_JIRA_ASSIGNEE,
}))
"""


def _probe(**overrides):
    env = {k: v for k, v in os.environ.items()
           if k not in _PROFILE_VARS and not k.startswith("SUPABASE_")}
    env.update(overrides)
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_defaults_reproduce_the_original_hardcoded_workspace():
    got = _probe()
    assert got["search_fields"] == [
        "summary", "issuetype", "assignee", "customfield_10015", "duedate",
        "customfield_10016", "parent", "status", "customfield_10175",
        "customfield_10176", "customfield_10020"]
    assert got["jql"] == 'project = "P" AND issuetype in (Epic, Story, Task) ORDER BY key ASC'
    assert {"customfield_10015", "customfield_10016", "customfield_10020",
            "customfield_10175", "customfield_10176"} <= set(got["changelog_fields"])
    assert got["start_mapping"] == ["customfield_10015"]
    assert got["f_start"] == "Timeline - Start"
    assert got["lark_base_url"] == "https://open.larksuite.com/open-apis"
    assert got["lark_host"] == "open.larksuite.com"
    assert got["bot"] == "ou_6c4fb657f0b844228990210a8fc789b5"
    assert got["public_url"] == "https://jira-lark-webhook.onrender.com"
    assert got["history_ts_suffix"] == "+07"
    assert got["allowed_types"] == ["Epic", "Story", "Task"]
    assert got["to_lark"] == {
        "Tawan Vongsombun": "Tawan", "Thet Swe Lin": "Lin",
        "Benyapha Kasemtanakitti": "Nurse", "Moe Pyae Pyae Kyaw": "Iris",
        "Waritsara Matnok": "Min"}
    assert got["to_jira"]["Nurse"] == "Benyapha Kasemtanakitti"


def test_env_overrides_reach_the_code_that_uses_them():
    got = _probe(
        JIRA_FIELD_START_DATE="customfield_20001",
        JIRA_FIELD_STORY_POINTS="customfield_20002",
        JIRA_FIELD_ACTUAL_START="customfield_20003",
        JIRA_FIELD_ACTUAL_END="customfield_20004",
        JIRA_FIELD_SPRINT="customfield_20005",
        LARK_API_BASE="https://open.feishu.cn/open-apis/",
        LARK_BOT_OPEN_ID="ou_other",
        PUBLIC_URL="https://sync.example.com/",
        TZ_OFFSET_HOURS="5.5",
        DEFAULT_SYNC_TYPES='["Epic", "Bug"]',
        ASSIGNEE_MAP='{"Ada Lovelace": "Ada"}',
    )
    assert got["search_fields"] == [
        "summary", "issuetype", "assignee", "customfield_20001", "duedate",
        "customfield_20002", "parent", "status", "customfield_20003",
        "customfield_20004", "customfield_20005"]
    assert got["jql"] == 'project = "P" AND issuetype in (Epic, Bug) ORDER BY key ASC'
    assert not [f for f in got["changelog_fields"] if f.startswith("customfield_10")]
    assert got["start_mapping"] == ["customfield_20001"]
    assert got["f_start"] == "Timeline - Start"
    assert got["lark_base_url"] == "https://open.feishu.cn/open-apis"
    assert got["lark_host"] == "open.feishu.cn"
    assert got["bot"] == "ou_other"
    assert got["public_url"] == "https://sync.example.com"
    assert got["history_ts_suffix"] == "+0530"
    assert got["allowed_types"] == ["Bug", "Epic"]
    assert got["to_lark"] == {"Ada Lovelace": "Ada"}
    assert got["to_jira"] == {"Ada": "Ada Lovelace"}


def test_blank_env_var_falls_back_to_the_default():
    assert _probe(JIRA_FIELD_SPRINT="", ASSIGNEE_MAP="")["search_fields"][-1] == "customfield_10020"


def test_invalid_json_fails_loudly_at_startup():
    env = dict(os.environ, ASSIGNEE_MAP="{not json")
    out = subprocess.run([sys.executable, "-c", "import config"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode != 0
    assert "ASSIGNEE_MAP is not valid JSON" in out.stderr
