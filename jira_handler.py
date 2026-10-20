"""Jira webhook events → Lark actions."""
import logging
import lark_api, index, dedup, history, field_mappings, config, projects
from config import (JF_START, JF_STORY_POINTS, JF_ACTUAL_START, JF_ACTUAL_END, JF_SPRINT)
from utils import (_jira_datetime_to_lark_ts, _jira_date_to_lark_ts,
                   _lark_ts_to_jira_date, _lark_text, _lark_select,
                   _lark_link_rid, _lark_multi, put)

log = logging.getLogger(__name__)


def _sprint_names(sprint_data) -> list:
    """All sprint names from Jira's customfield_10020 (list of sprint dicts)."""
    return [s.get("name") for s in (sprint_data or []) if s.get("name")]

RELEVANT_CHANGELOG_FIELDS = {
    "summary", "assignee", JF_STORY_POINTS,
    JF_ACTUAL_START, JF_ACTUAL_END, "status", "parent",
    JF_SPRINT,       # Sprint → Release
    JF_START,       # Start date → Timeline - Start
    "duedate",                 # Due date  → Timeline - End
    "IssueParentAssociation",  # Jira's actual changelog field name for a
                               # parent change (fieldId is None) — without
                               # this the gate returned early and parent
                               # changes synced nothing, with no log.
}


def process(event: str, issue: dict, changelog: dict, cfg: dict) -> None:
    with projects.use(cfg["PROJECT_ID"] if "PROJECT_ID" in cfg else projects.current_id()):
        _process(event, issue, changelog, cfg)


def _process(event: str, issue: dict, changelog: dict, cfg: dict) -> None:
    key = issue.get("key", "")
    itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
    log.info(f"jira_handler: event={event} key={key}")
    missing = field_mappings.missing_required()
    if missing:
        log.warning(f"jira_handler: skipping {event} {key} — project "
                    f"{projects.current_id()} has no mapping for required {missing}")
        field_mappings.report_skipped(missing, "jira_handler")
        return
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
    lark_assignee, assignee_unmapped = config.lark_assignee_for(assignee_name)
    sp_num = _sp_to_num(jf.get(JF_STORY_POINTS))
    jira_status = (jf.get("status") or {}).get("name")
    actual_start = _jira_datetime_to_lark_ts(jf.get(JF_ACTUAL_START))
    actual_end = _jira_datetime_to_lark_ts(jf.get(JF_ACTUAL_END))
    start_ts = _jira_date_to_lark_ts(jf.get(JF_START))
    end_ts = _jira_date_to_lark_ts(jf.get("duedate"))
    parent_jira_key = (jf.get("parent") or {}).get("key")
    parent_record_id = index._jira_to_lark.get(parent_jira_key) if parent_jira_key else None
    sprint_data = jf.get(JF_SPRINT) or []
    sprint_names = _sprint_names(sprint_data)

    # Only mapped fields are written (put skips an unmapped field).
    fields: dict = {}
    put(fields, field_mappings.F_TITLE,    jf.get("summary", ""))
    put(fields, field_mappings.F_JIRA_KEY, key)
    put(fields, field_mappings.F_JIRA_URL, f"https://{cfg['JIRA_DOMAIN']}/browse/{key}")
    put(fields, field_mappings.F_TYPE,     itype)
    if lark_assignee:        put(fields, field_mappings.F_ASSIGNEE,     [lark_assignee])
    if sp_num is not None:   put(fields, field_mappings.F_MD,           sp_num)
    if jira_status:      put(fields, field_mappings.F_JIRA_STATUS,  jira_status)
    if actual_start:     put(fields, field_mappings.F_ACTUAL_START, actual_start)
    if actual_end:       put(fields, field_mappings.F_ACTUAL_END,   actual_end)
    if start_ts:         put(fields, field_mappings.F_START,        start_ts)
    if end_ts:           put(fields, field_mappings.F_END,          end_ts)
    if parent_record_id: put(fields, field_mappings.F_PARENT,       [parent_record_id])
    if sprint_names:     put(fields, field_mappings.F_RELEASE,      sprint_names)

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rid = lark_api.create_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], fields)
    dedup.mark(f"lark:{rid}")
    index.add(key, rid)
    log.info(f"jira_handler: created Lark {rid} from Jira {key}")
    history.record(direction="jira→lark", event="created", jira_key=key, lark_id=rid,
                   description=f"Created {itype}: \"{jf.get('summary', '')}\"",
                   type=itype)
    if assignee_unmapped and field_mappings.F_ASSIGNEE:
        history.warn_unmapped_assignee(jira_key=key, name=assignee_name, lark_id=rid,
                                       outcome="Lark Assignee left empty", type=itype)


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

    # Fetch current Lark state — only write a field if the value actually differs.
    # Goes through the value cache (populated by every read/write/webhook path);
    # the call falls through to get_record on cache miss or expiry.
    try:
        lark_rec = lark_api.get_cached_or_fetch_record(
            token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
        lark_fields = lark_rec.get("fields", {})
    except Exception as e:
        log.warning(f"jira_handler: could not fetch Lark {record_id}: {e} — skipping comparison")
        lark_fields = {}

    updates: dict = {}
    unmapped_assignee = None
    for item in items:
        field = item.get("fieldId") or item.get("field")
        to_str = item.get("toString")
        to_raw = item.get("to")

        if field == "summary":
            if to_str is not None and to_str != _lark_text(lark_fields.get(field_mappings.F_TITLE)):
                put(updates, field_mappings.F_TITLE, to_str)

        elif field == "assignee":
            # Unassigned or not in the people list -> clear the Lark Assignee.
            lark_a, unmapped = config.lark_assignee_for(to_str)
            if lark_a != _lark_select(lark_fields.get(field_mappings.F_ASSIGNEE)):
                put(updates, field_mappings.F_ASSIGNEE, [lark_a] if lark_a else None)
                if unmapped and field_mappings.F_ASSIGNEE in updates:
                    unmapped_assignee = to_str

        elif field == JF_STORY_POINTS:
            sp_num = _sp_to_num(to_str)
            cur = lark_fields.get(field_mappings.F_MD)
            # Lark returns Number fields as strings ("3"); parse via _sp_to_num
            # so the value-compare converges (otherwise every story-point-bearing
            # update re-wrote R. MD — same class of phantom write reconcile hit).
            cur_num = _sp_to_num(cur)
            if sp_num != cur_num:
                put(updates, field_mappings.F_MD, sp_num)  # may be None to clear

        elif field == JF_ACTUAL_START:
            ts = _jira_datetime_to_lark_ts(to_raw or to_str)
            if ts is not None and ts != lark_fields.get(field_mappings.F_ACTUAL_START):
                put(updates, field_mappings.F_ACTUAL_START, ts)

        elif field == JF_ACTUAL_END:
            ts = _jira_datetime_to_lark_ts(to_raw or to_str)
            if ts is not None and ts != lark_fields.get(field_mappings.F_ACTUAL_END):
                put(updates, field_mappings.F_ACTUAL_END, ts)

        elif field == "status":
            if to_str and to_str != _lark_select(lark_fields.get(field_mappings.F_JIRA_STATUS)):
                put(updates, field_mappings.F_JIRA_STATUS, to_str)

        elif field == JF_START:  # Jira Start date → Timeline - Start
            # `ts` may legitimately be None here (Jira Start date cleared) — a
            # changelog item for this field only exists because it actually
            # changed, so None is as authoritative a "new value" as a real
            # timestamp. Requiring `ts is not None` used to silently drop
            # clears (mirrors the equivalent lark_handler bug for Lark→Jira).
            ts = _jira_date_to_lark_ts(to_raw or to_str)
            if (ts != lark_fields.get(field_mappings.F_START)
                    and not dedup.is_ours(
                        dedup.date_echo_key(key, "start", _lark_ts_to_jira_date(ts)))):
                put(updates, field_mappings.F_START, ts)  # may be None to clear

        elif field == "duedate":  # Jira Due date → Timeline - End
            ts = _jira_date_to_lark_ts(to_raw or to_str)
            if (ts != lark_fields.get(field_mappings.F_END)
                    and not dedup.is_ours(
                        dedup.date_echo_key(key, "end", _lark_ts_to_jira_date(ts)))):
                put(updates, field_mappings.F_END, ts)  # may be None to clear

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
    if field_mappings.F_PARENT is None:
        pass  # parent not mapped in this project — never synced
    elif parent_record_id and _lark_link_rid(lark_fields.get(field_mappings.F_PARENT)) != parent_record_id:
        put(updates, field_mappings.F_PARENT, [parent_record_id])
    elif parent_change and parent_jira_key and not parent_record_id:
        # Parent changed but its Lark record isn't linked yet — never silently
        # drop it (a parent that's set in Jira but missing in Lark is exactly
        # the kind of divergence the Data Integrity Rule forbids hiding). Flag
        # it so it's logged; the reconcile loop repairs once the parent syncs.
        parent_deferred = parent_jira_key

    # Reconcile Release against Jira's CURRENT sprint on every update — like
    # parent, not only when a sprint changelog item is present. Jira sprint is
    # the source of truth for "which sprint". Without this, a non-sprint edit
    # (sub-task add, man-day change) leaves a stale Lark Release; the
    # Lark→Jira sprint-move path then pushes that wrong value back and moves
    # the Jira card to the wrong sprint (silent data corruption). Value-compare
    # as a set so this can't cause a redundant write / loop. An empty Jira
    # sprint on its own is ambiguous (Lark Release may hold a value with no
    # matching sprint), so it only clears Lark when THIS event carries a
    # Sprint changelog item — i.e. the card was actually moved to the backlog.
    # Gating on `if jira_sprints` alone silently dropped every sprint→backlog
    # move (VR-741 et al., Beta 1.6, 2026-09-30).
    jira_sprints = _sprint_names(issue["fields"].get(JF_SPRINT))
    lark_release = _lark_multi(lark_fields.get(field_mappings.F_RELEASE))
    sprint_changed = any((it.get("fieldId") or it.get("field")) == JF_SPRINT
                         for it in items)
    if jira_sprints and set(jira_sprints) != set(lark_release):
        put(updates, field_mappings.F_RELEASE, jira_sprints)
    elif not jira_sprints and sprint_changed and lark_release:
        put(updates, field_mappings.F_RELEASE, None)  # moved to backlog → clear

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
            # Lark returns Number fields as strings; parse current value the same
            # way so a custom number mapping doesn't re-write every update.
            cur_num = _sp_to_num(cur)
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
    # Echo-suppress: the Lark write above will fire a Lark→Jira webhook for
    # these exact dates; mark them so lark_handler skips re-propagating (breaks
    # a bidirectional conflict ping-pong). Keyed on the canonical Jira date.
    for slot, fld in (("start", field_mappings.F_START), ("end", field_mappings.F_END)):
        if fld in updates:
            dedup.mark(dedup.date_echo_key(key, slot, _lark_ts_to_jira_date(updates[fld])))
    desc = ", ".join(f"{item.get('field')}: {item.get('toString','')}" for item in items
                     if (item.get("fieldId") or item.get("field")) in RELEVANT_CHANGELOG_FIELDS)
    log.info(f"jira_handler: updated Lark {record_id} from Jira {key} — {desc}")
    itype = ((issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
    history.record(direction="jira→lark", event="updated", jira_key=key, lark_id=record_id,
                   description=desc or "updated", type=itype)
    if unmapped_assignee:
        history.warn_unmapped_assignee(jira_key=key, name=unmapped_assignee,
                                       lark_id=record_id, type=itype)


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
        # Decorative-only fetch for the log/history row — route through the
        # cache; on a hit we get title + type without a Lark API call.
        rec = lark_api.get_cached_or_fetch_record(
            token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], record_id)
        title = _lark_text(rec["fields"].get(field_mappings.F_TITLE)) or ""
        if not itype:
            itype = _lark_select(rec["fields"].get(field_mappings.F_TYPE)) or ""
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
