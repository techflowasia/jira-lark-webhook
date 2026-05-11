"""Jira REST API v3 client."""
import requests
from requests.auth import HTTPBasicAuth


def _auth(cfg: dict) -> HTTPBasicAuth:
    return HTTPBasicAuth(cfg["JIRA_EMAIL"], cfg["JIRA_TOKEN"])


def _url(cfg: dict, path: str) -> str:
    return f"https://{cfg['JIRA_DOMAIN']}{path}"


def fetch_all_issues(cfg: dict) -> list:
    issues, next_token = [], None
    fields = ["summary", "issuetype", "assignee", "customfield_10015",
              "duedate", "customfield_10016", "parent", "status",
              "customfield_10175", "customfield_10176"]
    while True:
        payload = {
            "jql": f"project={cfg['JIRA_PROJECT']} AND issuetype in (Epic,Story,Task) ORDER BY key ASC",
            "maxResults": 100,
            "fields": fields,
        }
        if next_token:
            payload["nextPageToken"] = next_token
        resp = requests.post(_url(cfg, "/rest/api/3/search/jql"),
                             json=payload, auth=_auth(cfg))
        resp.raise_for_status()
        data = resp.json()
        issues.extend(data.get("issues", []))
        if data.get("isLast", True) or not data.get("issues"):
            break
        next_token = data.get("nextPageToken")
    return issues


def get_issue(cfg: dict, key: str) -> dict:
    resp = requests.get(_url(cfg, f"/rest/api/3/issue/{key}"), auth=_auth(cfg))
    resp.raise_for_status()
    return resp.json()


def get_account_ids(cfg: dict) -> dict:
    resp = requests.post(_url(cfg, "/rest/api/3/search/jql"),
                         json={"jql": f"project={cfg['JIRA_PROJECT']} AND assignee is not EMPTY",
                               "maxResults": 200, "fields": ["assignee"]},
                         auth=_auth(cfg))
    resp.raise_for_status()
    return {i["fields"]["assignee"]["displayName"]: i["fields"]["assignee"]["accountId"]
            for i in resp.json().get("issues", [])
            if i["fields"].get("assignee")}


def create_issue(cfg: dict, issuetype: str, summary: str,
                 start_date=None, due_date=None,
                 assignee_id=None, parent_key=None) -> str:
    fields = {
        "project": {"key": cfg["JIRA_PROJECT"]},
        "issuetype": {"name": issuetype},
        "summary": summary,
    }
    if start_date:  fields["customfield_10015"] = start_date
    if due_date:    fields["duedate"] = due_date
    if assignee_id: fields["assignee"] = {"id": assignee_id}
    if parent_key:  fields["parent"] = {"key": parent_key}
    resp = requests.post(_url(cfg, "/rest/api/3/issue"),
                         json={"fields": fields}, auth=_auth(cfg))
    resp.raise_for_status()
    return resp.json()["key"]


def update_issue(cfg: dict, key: str, fields: dict) -> None:
    if not fields:
        return
    resp = requests.put(_url(cfg, f"/rest/api/3/issue/{key}"),
                        json={"fields": fields}, auth=_auth(cfg))
    resp.raise_for_status()


def delete_issue(cfg: dict, key: str) -> None:
    resp = requests.delete(_url(cfg, f"/rest/api/3/issue/{key}"), auth=_auth(cfg))
    resp.raise_for_status()


def move_story(cfg: dict, story_key: str, new_parent_key: str) -> None:
    update_issue(cfg, story_key, {"parent": {"key": new_parent_key}})


def get_project_versions(cfg: dict) -> list:
    resp = requests.get(_url(cfg, f"/rest/api/3/project/{cfg['JIRA_PROJECT']}/versions"),
                        auth=_auth(cfg))
    resp.raise_for_status()
    return [{"id": v["id"], "name": v["name"]} for v in resp.json()]


def get_board_id(cfg: dict) -> "str | None":
    resp = requests.get(_url(cfg, "/rest/agile/1.0/board"),
                        params={"projectKeyOrId": cfg["JIRA_PROJECT"]},
                        auth=_auth(cfg))
    if not resp.ok:
        return None
    values = resp.json().get("values", [])
    return str(values[0]["id"]) if values else None


def get_board_sprints(cfg: dict, board_id: str) -> list:
    sprints, start_at = [], 0
    while True:
        resp = requests.get(_url(cfg, f"/rest/agile/1.0/board/{board_id}/sprint"),
                            params={"startAt": start_at, "maxResults": 50},
                            auth=_auth(cfg))
        resp.raise_for_status()
        data = resp.json()
        for s in data.get("values", []):
            sprints.append({"id": s["id"], "name": s["name"]})
        if data.get("isLast", True):
            break
        start_at += len(data.get("values", []))
    return sprints


def move_to_sprint(cfg: dict, sprint_id: int, issue_key: str) -> None:
    resp = requests.post(_url(cfg, f"/rest/agile/1.0/sprint/{sprint_id}/issue"),
                         json={"issues": [issue_key]}, auth=_auth(cfg))
    resp.raise_for_status()
