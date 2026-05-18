"""Jira webhook events → Lark actions."""
import logging
import lark_api, index, dedup, history, field_mappings, config
from config import (F_TITLE, F_JIRA_KEY, F_JIRA_URL, F_TYPE, F_ASSIGNEE,
                    F_MD, F_JIRA_STATUS, F_ACTUAL_START, F_ACTUAL_END, F_PARENT,
                    F_RELEASE, F_START, F_END, JIRA_TO_LARK_ASSIGNEE)
from utils import (_jira_datetime_to_lark_ts, _jira_date_to_lark_ts,
                   _lark_text, _lark_select, _lark_link_rid, _lark_multi)

log = logging.getLogger(__name__)


def _sprint_names(sprint_data) -> list:
    """All sprint names from Jira's customfield_10020 (list of sprint dicts)."""
    return [s.get("name") for s in (sprint_data or []) if s.get("name")]


def _split_sprint_changelog(to_str: str) -> list:
    """Jira's sprint changelog `toString` is comma-joined ("A, B"). Split it
    into individual option names so Lark's multi-select Release gets separate
    values instead of one bogus combined "A, B" option."""
    return [s.strip() for s in (to_str or "").split(",") if s.strip()]

RELEVANT_CHANGELOG_FIELDS = {
    "summary", "assignee", "customfield_10016",
    "customfield_10175", "customfield_10176", "status", "parent",
    "customfield_10020",       # Sprint → Release
    "customfield_10015",       # Start date → Timeline - Start
    "duedate",                 # Due date  → Timeline - End
    "IssueParentAssociation",  # Jira's actual changelog field name for a
                               # parent change (fieldId is None) — without
                               # this the gate returned early and parent
                               # changes synced nothing, with no log.
}


def process(event: str, issue: dict, changelog: dict, cfg: dict) -> None:
    key = issue.get("key", "")
    itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
    log.info(f"jira_handler: event={event} key={key}")
    try:
        if event == "jira:issue_created":
            _handle_create(issue, cfg)
        elif event == "jira:issue_updated":
            _handle_update(issue, changelog, cfg)
        elif event == "jira:issue_deleted":
            _handle_delete(key, itype, cfg)
    except Exception as e:
        log.error(f"jira_handler.{event} key={key}: {e}", exc_info=True)
        history.record(direction="jira→lark", event=event, jira_key=key,
                       description=str(e), status="error", error=str(e), type=itype)


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
    sp_num = _sp_to_num(jf.get("customfield_10016"))
    jira_status = (jf.get("status") or {}).get("name")
    actual_start = _jira_datetime_to_lark_ts(jf.get("customfield_10175"))
    actual_end = _jira_datetime_to_lark_ts(jf.get("customfield_10176"))
    start_ts = _jira_date_to_lark_ts(jf.get("customfield_10015"))
    end_ts = _jira_date_to_lark_ts(jf.get("duedate"))
    parent_jira_key = (jf.get("parent") or {}).get("key")
    parent_record_id = index._jira_to_lark.get(parent_jira_key) if parent_jira_key else None
    sprint_data = jf.get("customfield_10020") or []
    sprint_names = _sprint_names(sprint_data)

    fields = {
        F_TITLE:    jf.get("summary", ""),
        F_JIRA_KEY: key,
        F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{key}",
        F_TYPE:     itype,
    }
    if lark_assignee:        fields[F_ASSIGNEE]     = [lark_assignee]
    if sp_num is not None:   fields[F_MD]           = sp_num
    if jira_status:      fields[F_JIRA_STATUS]  = jira_status
    if actual_start:     fields[F_ACTUAL_START] = actual_start
    if actual_end:       fields[F_ACTUAL_END]   = actual_end
    if start_ts:         fields[F_START]        = start_ts
    if end_ts:           fields[F_END]          = end_ts
    if parent_record_id: fields[F_PARENT]       = [parent_record_id]
    if sprint_names:     fields[F_RELEASE]      = sprint_names

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rid = lark_api.create_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], fields)
    dedup.mark(f"lark:{rid}")
    index.add(key, rid)
    log.info(f"jira_handler: created Lark {rid} from Jira {key}")
    history.record(direction="jira→lark", event="created", jira_key=key, lark_id=rid,
                   description=f"Created {itype}: \"{jf.get('summary', '')}\"",
                   type=itype)


def _handle_update(issue: dict, changelog: dict, cfg: dict) -> None:
    key = issue["key"]
    record_id = index._jira_to_lark.get(key)
    if not record_id:
        log.warning(f"jira_handler: {key} not in index — skipping")
        return

    items = changelog.get("items", [])
    # Custom (dashboard-configured) Jira→Lark mappings count as relevant too —
    # otherwise a changelog with ONLY a custom field (e.g. customfield_10178
    # "QA Man day") hits this early-return and is silently dropped, no log.
    custom_j2l = {m["jira_field"]: m for m in field_mappings.get_custom_jira_to_lark()}
    _relevant = RELEVANT_CHANGELOG_FIELDS | set(custom_j2l)
    if not any((item.get("fieldId") or item.get("field")) in _relevant for item in items):
        return

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])

    # Fetch current Lark state — only write a field if the value actually differs
    try:
        lark_rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
        lark_fields = lark_rec.get("fields", {})
    except Exception as e:
        log.warning(f"jira_handler: could not fetch Lark {record_id}: {e} — skipping comparison")
        lark_fields = {}

    updates: dict = {}
    for item in items:
        field = item.get("fieldId") or item.get("field")
        to_str = item.get("toString")
        to_raw = item.get("to")

        if field == "summary":
            if to_str is not None and to_str != _lark_text(lark_fields.get(F_TITLE)):
                updates[F_TITLE] = to_str

        elif field == "assignee":
            lark_a = JIRA_TO_LARK_ASSIGNEE.get(to_str) if to_str else None
            if lark_a != _lark_select(lark_fields.get(F_ASSIGNEE)):
                updates[F_ASSIGNEE] = [lark_a] if lark_a else None

        elif field == "customfield_10016":
            sp_num = _sp_to_num(to_str)
            cur = lark_fields.get(F_MD)
            cur_num = cur if isinstance(cur, (int, float)) else None
            if sp_num != cur_num:
                updates[F_MD] = sp_num  # may be None to clear

        elif field == "customfield_10175":
            ts = _jira_datetime_to_lark_ts(to_raw or to_str)
            if ts is not None and ts != lark_fields.get(F_ACTUAL_START):
                updates[F_ACTUAL_START] = ts

        elif field == "customfield_10176":
            ts = _jira_datetime_to_lark_ts(to_raw or to_str)
            if ts is not None and ts != lark_fields.get(F_ACTUAL_END):
                updates[F_ACTUAL_END] = ts

        elif field == "status":
            if to_str and to_str != _lark_select(lark_fields.get(F_JIRA_STATUS)):
                updates[F_JIRA_STATUS] = to_str

        elif field == "customfield_10020":
            if to_str:
                new_releases = _split_sprint_changelog(to_str)
                current = set(_lark_multi(lark_fields.get(F_RELEASE)))
                if new_releases and set(new_releases) != current:
                    updates[F_RELEASE] = new_releases

        elif field == "customfield_10015":  # Jira Start date → Timeline - Start
            ts = _jira_date_to_lark_ts(to_raw or to_str)
            if ts is not None and ts != lark_fields.get(F_START):
                updates[F_START] = ts

        elif field == "duedate":  # Jira Due date → Timeline - End
            ts = _jira_date_to_lark_ts(to_raw or to_str)
            if ts is not None and ts != lark_fields.get(F_END):
                updates[F_END] = ts

    # Reconcile parent on every update. Jira fires the parent-change changelog
    # as field 'IssueParentAssociation' (fieldId None) — `toString` is the new
    # parent key. Resolve from issue.fields.parent first, fall back to that
    # changelog value if the snapshot lacks it.
    parent_change = next(
        (it for it in items
         if (it.get("fieldId") or it.get("field")) in ("parent", "IssueParentAssociation")),
        None)
    parent_jira_key = (issue["fields"].get("parent") or {}).get("key")
    if not parent_jira_key and parent_change:
        parent_jira_key = parent_change.get("toString") or None
    parent_record_id = index._jira_to_lark.get(parent_jira_key) if parent_jira_key else None
    parent_deferred = None
    if parent_record_id and _lark_link_rid(lark_fields.get(F_PARENT)) != parent_record_id:
        updates[F_PARENT] = [parent_record_id]
    elif parent_change and parent_jira_key and not parent_record_id:
        # Parent changed but its Lark record isn't linked yet — never silently
        # drop it (a parent that's set in Jira but missing in Lark is exactly
        # the kind of divergence the Data Integrity Rule forbids hiding). Flag
        # it so it's logged; the reconcile loop repairs once the parent syncs.
        parent_deferred = parent_jira_key

    # Apply custom (dashboard-configured) Jira → Lark mappings from changelog.
    # Coerce by the mapping's field_type — writing a raw string to a Lark
    # Number field fails with NumberFieldConvFail. Value-compare so a number
    # like 5 vs 5.0 (or unchanged value) doesn't cause a redundant write/loop.
    for item in items:
        jf = item.get("fieldId") or item.get("field")
        if jf not in custom_j2l or jf in RELEVANT_CHANGELOG_FIELDS:
            continue
        m = custom_j2l[jf]
        name = m["lark_field"]
        ft = m.get("field_type", "text")
        raw = item.get("to")
        to_str = item.get("toString")
        if ft == "number":
            val = _sp_to_num(raw if raw not in (None, "") else to_str)
            cur = lark_fields.get(name)
            cur_num = cur if isinstance(cur, (int, float)) else None
            if val != cur_num:
                updates[name] = val  # may be None to clear
        elif ft == "date":
            ts = _jira_date_to_lark_ts(raw or to_str)
            if ts is not None and ts != lark_fields.get(name):
                updates[name] = ts
        else:
            if to_str is not None and to_str != _lark_text(lark_fields.get(name)):
                updates[name] = to_str

    if not updates:
        if parent_deferred:
            itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
            log.info(f"jira_handler: {key} parent → {parent_deferred} deferred "
                     f"(parent not yet linked in Lark)")
            history.record(direction="jira→lark", event="updated", jira_key=key,
                           lark_id=record_id,
                           description=f"Parent → {parent_deferred} (deferred: "
                                       f"parent not yet linked in Lark)",
                           status="skipped", type=itype)
        return

    lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"],
                           record_id, updates)
    desc = ", ".join(f"{item.get('field')}: {item.get('toString','')}" for item in items
                     if (item.get("fieldId") or item.get("field")) in RELEVANT_CHANGELOG_FIELDS)
    log.info(f"jira_handler: updated Lark {record_id} from Jira {key} — {desc}")
    itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
    history.record(direction="jira→lark", event="updated", jira_key=key, lark_id=record_id,
                   description=desc or "updated", type=itype)


def _handle_delete(key: str, itype: str, cfg: dict) -> None:
    if dedup.is_ours(f"jira:{key}"):
        log.info(f"jira_handler: skipping delete {key} — dedup (originated from Lark)")
        return
    record_id = index._jira_to_lark.get(key)
    if not record_id:
        return
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    title = ""
    try:
        rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
        title = _lark_text(rec["fields"].get(F_TITLE)) or ""
        if not itype:
            itype = _lark_select(rec["fields"].get(F_TYPE)) or ""
    except Exception:
        pass
    dedup.mark(f"lark_delete:{record_id}")
    try:
        lark_api.delete_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
    except Exception as e:
        log.error(f"Delete Lark {record_id}: {e}")
    index.remove_by_jira(key)
    log.info(f'jira_handler: deleted Lark {record_id} (Jira {key} deleted) — "{title}"')
    history.record(direction="jira→lark", event="deleted", jira_key=key, lark_id=record_id,
                   description=f'Deleted Lark record for {key}: "{title}"', type=itype)


def _sp_to_num(val):
    """Story points → numeric value Lark Bitable accepts (int when whole, else float).

    Lark number fields reject strings (NumberFieldConvFail). Returns None when
    Jira sends null/empty/non-numeric so the caller can decide whether to clear
    or skip."""
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f
