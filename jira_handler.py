"""Jira webhook events → Lark actions."""
import logging
import lark_api, index, dedup, history, field_mappings, config
from config import (F_TITLE, F_JIRA_KEY, F_JIRA_URL, F_TYPE, F_ASSIGNEE,
                    F_MD, F_JIRA_STATUS, F_ACTUAL_START, F_ACTUAL_END, F_PARENT,
                    JIRA_TO_LARK_ASSIGNEE)
from utils import _jira_datetime_to_lark_ts

log = logging.getLogger(__name__)

RELEVANT_CHANGELOG_FIELDS = {
    "summary", "assignee", "customfield_10016",
    "customfield_10175", "customfield_10176", "status", "parent",
}


def process(event: str, issue: dict, changelog: dict, cfg: dict) -> None:
    key = issue.get("key", "")
    log.info(f"jira_handler: event={event} key={key}")
    try:
        if event == "jira:issue_created":
            _handle_create(issue, cfg)
        elif event == "jira:issue_updated":
            _handle_update(issue, changelog, cfg)
        elif event == "jira:issue_deleted":
            _handle_delete(key, cfg)
    except Exception as e:
        log.error(f"jira_handler.{event} key={key}: {e}", exc_info=True)
        history.record(direction="jira→lark", event=event, jira_key=key,
                       description=str(e), status="error", error=str(e))


def _handle_create(issue: dict, cfg: dict) -> None:
    key = issue["key"]
    if dedup.is_ours(f"jira:{key}"):
        return
    if key in index._jira_to_lark:
        return  # already linked

    jf = issue["fields"]
    itype = jf["issuetype"]["name"]
    if itype not in config.get_allowed_jira_types():
        return

    assignee_name = (jf.get("assignee") or {}).get("displayName")
    lark_assignee = JIRA_TO_LARK_ASSIGNEE.get(assignee_name) if assignee_name else None
    sp = jf.get("customfield_10016")
    sp_str = _sp_to_str(sp)
    jira_status = (jf.get("status") or {}).get("name")
    actual_start = _jira_datetime_to_lark_ts(jf.get("customfield_10175"))
    actual_end = _jira_datetime_to_lark_ts(jf.get("customfield_10176"))
    parent_jira_key = (jf.get("parent") or {}).get("key")
    parent_record_id = index._jira_to_lark.get(parent_jira_key) if parent_jira_key else None

    fields = {
        F_TITLE:    jf.get("summary", ""),
        F_JIRA_KEY: key,
        F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{key}",
        F_TYPE:     itype,
    }
    if lark_assignee:    fields[F_ASSIGNEE]     = [lark_assignee]
    if sp_str:           fields[F_MD]           = sp_str
    if jira_status:      fields[F_JIRA_STATUS]  = jira_status
    if actual_start:     fields[F_ACTUAL_START] = actual_start
    if actual_end:       fields[F_ACTUAL_END]   = actual_end
    if parent_record_id: fields[F_PARENT]       = [parent_record_id]

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rid = lark_api.create_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], fields)
    dedup.mark(f"lark:{rid}")
    index.add(key, rid)
    log.info(f"jira_handler: created Lark {rid} from Jira {key}")
    history.record(direction="jira→lark", event="created", jira_key=key, lark_id=rid,
                   description=f"Created {itype}: \"{jf.get('summary', '')}\"" )


def _handle_update(issue: dict, changelog: dict, cfg: dict) -> None:
    key = issue["key"]
    record_id = index._jira_to_lark.get(key)
    if not record_id:
        return
    if dedup.is_ours(f"jira:{key}"):
        return

    items = changelog.get("items", [])
    if not any(item.get("field") in RELEVANT_CHANGELOG_FIELDS for item in items):
        return

    updates: dict = {}
    for item in items:
        field = item.get("field")
        to_str = item.get("toString")
        to_raw = item.get("to")

        if field == "summary":
            if to_str is not None:
                updates[F_TITLE] = to_str

        elif field == "assignee":
            lark_a = JIRA_TO_LARK_ASSIGNEE.get(to_str) if to_str else None
            updates[F_ASSIGNEE] = [lark_a] if lark_a else None

        elif field == "customfield_10016":
            sp_str = _sp_to_str(to_str)
            if sp_str is not None:
                updates[F_MD] = sp_str

        elif field == "customfield_10175":
            ts = _jira_datetime_to_lark_ts(to_raw or to_str)
            if ts is not None:
                updates[F_ACTUAL_START] = ts

        elif field == "customfield_10176":
            ts = _jira_datetime_to_lark_ts(to_raw or to_str)
            if ts is not None:
                updates[F_ACTUAL_END] = ts

        elif field == "status":
            if to_str:
                updates[F_JIRA_STATUS] = to_str

        elif field == "parent":
            parent_record_id = index._jira_to_lark.get(to_str) if to_str else None
            if parent_record_id:
                updates[F_PARENT] = [parent_record_id]

    # Apply custom (non-system) Jira → Lark mappings from changelog
    custom_j2l = {m["jira_field"]: m for m in field_mappings.get_custom_jira_to_lark()}
    for item in items:
        jf = item.get("field")
        if jf in custom_j2l and jf not in RELEVANT_CHANGELOG_FIELDS:
            m = custom_j2l[jf]
            to_str = item.get("toString")
            if to_str is not None:
                updates[m["lark_field"]] = to_str

    if not updates:
        return

    dedup.mark(f"lark:{record_id}")
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"],
                           record_id, updates)
    desc = ", ".join(f"{item.get('field')}: {item.get('toString','')}" for item in items
                     if item.get("field") in RELEVANT_CHANGELOG_FIELDS)
    log.info(f"jira_handler: updated Lark {record_id} from Jira {key} — {desc}")
    history.record(direction="jira→lark", event="updated", jira_key=key, lark_id=record_id,
                   description=desc or "updated")


def _handle_delete(key: str, cfg: dict) -> None:
    record_id = index._jira_to_lark.get(key)
    if not record_id:
        return
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    title = ""
    try:
        rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
        title = _lark_text(rec["fields"].get(F_TITLE)) or ""
    except Exception:
        pass
    dedup.mark(f"lark:{record_id}")
    try:
        lark_api.delete_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
    except Exception as e:
        log.error(f"Delete Lark {record_id}: {e}")
    index.remove_by_jira(key)
    log.info(f'jira_handler: deleted Lark {record_id} (Jira {key} deleted) — "{title}"')
    history.record(direction="jira→lark", event="deleted", jira_key=key, lark_id=record_id,
                   description=f'Deleted Lark record for {key}: "{title}"')


def _sp_to_str(val) -> "str | None":
    if val is None:
        return None
    try:
        f = float(val)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return None
