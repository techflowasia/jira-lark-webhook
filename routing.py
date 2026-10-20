"""Pure routing of inbound webhooks to the owning Project.

A webhook routed to the wrong project would write one project's data into
another's, so every function returns None unless the match is unambiguous.
"""
import projects


def route_jira(body: dict) -> "projects.Project | None":
    issue = (body or {}).get("issue") or {}
    key = ((issue.get("fields") or {}).get("project") or {}).get("key")
    if not key:
        issue_key = issue.get("key") or ""
        if "-" in issue_key:
            key = issue_key.rsplit("-", 1)[0]
    if not key:
        return None
    return projects.find_by_jira_key(key)


def route_lark_event(event: dict) -> "projects.Project | None":
    event = event or {}
    base, table = event.get("file_token"), event.get("table_id")
    if not base or not table:
        return None
    return projects.find_by_lark(base, table)


def route_lark_auto(body: dict, project_id: "int | None") -> "projects.Project | None":
    body = body or {}
    table_id = body.get("table_id")
    if project_id is not None:
        p = projects.get(project_id)
        if p is None:
            return None
        if table_id and table_id != p.lark_table_id:
            return None
        return p
    p1 = projects.get(1)
    if p1 is None:
        return None
    if table_id:
        return projects.find_by_lark(p1.lark_base_token, table_id)
    return p1


def is_live(p: "projects.Project | None") -> bool:
    return p is not None and p.sync_enabled and p.setup_state == "active"
