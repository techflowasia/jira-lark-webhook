"""Lark Base events → Jira actions."""
import time
import logging
import lark_api, jira_api, index, dedup, history, field_mappings, config
from config import (F_TITLE, F_START, F_END, F_ASSIGNEE, F_JIRA_KEY, F_JIRA_URL,
                    F_TYPE, F_PARENT, F_RELEASE, LARK_TO_JIRA_ASSIGNEE)
from utils import _lark_text, _lark_select, _lark_ts_to_jira_date, _norm, _lark_link_rid

log = logging.getLogger(__name__)

_account_ids_cache: dict = {"data": None, "expires_at": 0}
_version_cache: dict = {"data": {}, "expires_at": 0}
_sprint_cache: dict = {"data": {}, "expires_at": 0}


def _get_account_ids(cfg: dict) -> dict:
    now = time.time()
    if _account_ids_cache["data"] and now < _account_ids_cache["expires_at"]:
        return _account_ids_cache["data"]
    data = jira_api.get_account_ids(cfg)
    _account_ids_cache.update({"data": data, "expires_at": now + 3600})
    return data


def _get_version_map(cfg: dict) -> dict:
    now = time.time()
    if _version_cache["data"] and now < _version_cache["expires_at"]:
        return _version_cache["data"]
    try:
        data = {_norm(v["name"]): v["id"] for v in jira_api.get_project_versions(cfg)}
    except Exception:
        data = {}
    _version_cache.update({"data": data, "expires_at": now + 3600})
    return data


def _get_sprint_map(cfg: dict) -> dict:
    now = time.time()
    if _sprint_cache["data"] and now < _sprint_cache["expires_at"]:
        return _sprint_cache["data"]
    try:
        bid = jira_api.get_board_id(cfg)
        data = ({_norm(s["name"]): s["id"] for s in jira_api.get_board_sprints(cfg, bid)}
                if bid else {})
    except Exception:
        data = {}
    _sprint_cache.update({"data": data, "expires_at": now + 3600})
    return data


def _resolve_parent(rec: dict) -> "str | None":
    parent_data = rec["fields"].get(F_PARENT) or []
    for item in parent_data:
        rid = _lark_link_rid([item])
        if rid:
            jk = index._lark_to_jira.get(rid)
            if jk:
                return jk
    return None


def process(action: dict, table_id: str, cfg: dict) -> None:
    act = action.get("action")
    rid = action.get("record_id", "")
    log.info(f"lark_handler: action={act} record_id={rid} table_id={table_id}")
    try:
        if act == "record_added":
            _handle_create(rid, table_id, cfg)
        elif act == "record_edited":
            _handle_update(rid, table_id, cfg)
        elif act == "record_deleted":
            _handle_delete(rid, cfg)
        else:
            log.warning(f"lark_handler: unknown action '{act}' for {rid}")
    except Exception as e:
        log.error(f"lark_handler.{act} rid={rid}: {e}", exc_info=True)
        jira_key = index._lark_to_jira.get(rid, "")
        history.record(direction="lark→jira", event=act or "unknown",
                       lark_id=rid, jira_key=jira_key,
                       description=f"Lark update error: {e}",
                       status="error", error=str(e))


def _handle_create(rid: str, table_id: str, cfg: dict) -> None:
    if dedup.is_ours(f"lark:{rid}"):
        log.info(f"lark_handler: skipping create {rid} — dedup")
        return

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
    log.info(f"lark_handler: record fields keys={list(rec['fields'].keys())}")

    if _lark_text(rec["fields"].get(F_JIRA_KEY)):
        log.info(f"lark_handler: skipping create {rid} — Jira Key already set")
        return

    itype = _lark_select(rec["fields"].get(F_TYPE))
    allowed = config.get_allowed_lark_types()
    if itype not in allowed:
        log.info(f"lark_handler: skipping create {rid} — type '{itype}' not in {allowed}")
        history.record(direction="lark→jira", event="created", lark_id=rid,
                       description=f"Skipped: type '{itype}' not in allowed types",
                       status="skipped")
        return

    title = _lark_text(rec["fields"].get(F_TITLE)) or f"[Lark] {rid}"
    start = _lark_ts_to_jira_date(rec["fields"].get(F_START))
    end = _lark_ts_to_jira_date(rec["fields"].get(F_END))
    parent_jira_key = _resolve_parent(rec) if itype in ("Story", "Task") else None

    assignee_lark = _lark_select(rec["fields"].get(F_ASSIGNEE))
    jira_name = LARK_TO_JIRA_ASSIGNEE.get(assignee_lark, "")
    assignee_id = _get_account_ids(cfg).get(jira_name)

    new_key = jira_api.create_issue(cfg, itype, title,
                                     start_date=start, due_date=end,
                                     assignee_id=assignee_id,
                                     parent_key=parent_jira_key)
    dedup.mark(f"jira:{new_key}")

    dedup.mark(f"lark:{rid}")
    lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid, {
        F_JIRA_KEY: new_key,
        F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{new_key}",
    })
    index.add(new_key, rid)
    log.info(f"lark_handler: created Jira {new_key} from Lark {rid}")
    history.record(direction="lark→jira", event="created", lark_id=rid,
                   jira_key=new_key, description=f"Created {itype}: \"{title}\"")


def _handle_update(rid: str, table_id: str, cfg: dict) -> None:
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)

    jira_key = index._lark_to_jira.get(rid)
    if not jira_key:
        # Not in index — check if the record itself has a Jira Key (auto-discover)
        jira_key = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if jira_key:
            index.add(jira_key, rid)
            log.info(f"lark_handler: auto-discovered link {rid} → {jira_key}")
        else:
            log.info(f"lark_handler: skipping update {rid} — not linked to Jira")
            return

    # Fetch current Jira state — only write a field if the value actually differs
    try:
        jira_issue = jira_api.get_issue(cfg, jira_key)
        jira_fields = (jira_issue or {}).get("fields", {})
    except Exception as e:
        log.warning(f"lark_handler: could not fetch Jira {jira_key}: {e} — skipping comparison")
        jira_fields = {}

    updates: dict = {}
    changed: list = []

    title = _lark_text(rec["fields"].get(F_TITLE))
    if title and title != jira_fields.get("summary"):
        updates["summary"] = title
        changed.append(f"Title: \"{title}\"")

    start = _lark_ts_to_jira_date(rec["fields"].get(F_START))
    if start and start != jira_fields.get("customfield_10015"):
        updates["customfield_10015"] = start
        changed.append(f"Start: {start}")

    end = _lark_ts_to_jira_date(rec["fields"].get(F_END))
    if end and end != jira_fields.get("duedate"):
        updates["duedate"] = end
        changed.append(f"Due: {end}")

    assignee_lark = _lark_select(rec["fields"].get(F_ASSIGNEE))
    if assignee_lark:
        jira_name = LARK_TO_JIRA_ASSIGNEE.get(assignee_lark, "")
        account_id = _get_account_ids(cfg).get(jira_name)
        if account_id:
            current_account_id = (jira_fields.get("assignee") or {}).get("accountId")
            if account_id != current_account_id:
                updates["assignee"] = {"id": account_id}
                changed.append(f"Assignee: {assignee_lark}")

    release_raw = (_lark_text(rec["fields"].get(F_RELEASE))
                   or _lark_select(rec["fields"].get(F_RELEASE)))
    if release_raw:
        vid = _get_version_map(cfg).get(_norm(release_raw))
        if vid:
            current_version_ids = [v["id"] for v in (jira_fields.get("fixVersions") or [])]
            if vid not in current_version_ids:
                updates["fixVersions"] = [{"id": vid}]
                changed.append(f"Release: {release_raw}")

    parent_jira_key = _resolve_parent(rec)
    if parent_jira_key:
        current_parent = (jira_fields.get("parent") or {}).get("key")
        if parent_jira_key != current_parent:
            updates["parent"] = {"key": parent_jira_key}
            changed.append(f"Parent: {parent_jira_key}")

    # Apply custom (non-system) Lark → Jira mappings
    for m in field_mappings.get_custom_lark_to_jira():
        if m["jira_field"] == "customfield_10020":
            # Sprint is set via Agile API (move_to_sprint) — issue update rejects text values
            continue
        raw = rec["fields"].get(m["lark_field"])
        if raw is None:
            continue
        ft = m.get("field_type", "text")
        if ft == "date":
            val = _lark_ts_to_jira_date(raw)
        elif ft == "number":
            try:
                val = float(raw) if raw else None
            except (TypeError, ValueError):
                val = None
        else:
            val = _lark_text(raw) or _lark_select(raw)
        if val:
            updates[m["jira_field"]] = val
            changed.append(f"{m['jira_label'] or m['lark_field']}: {val}")

    if not updates:
        log.info(f"lark_handler: no relevant updates for {jira_key}")
        return

    log.info(f"lark_handler: sending to Jira {jira_key} fields={list(updates.keys())}")
    jira_api.update_issue(cfg, jira_key, updates)

    if release_raw:
        sid = _get_sprint_map(cfg).get(_norm(release_raw))
        if sid:
            try:
                jira_api.move_to_sprint(cfg, sid, jira_key)
            except Exception as e:
                log.warning(f"Sprint move {jira_key}: {e}")

    desc = ", ".join(changed)
    log.info(f"lark_handler: updated Jira {jira_key} — {desc}")
    history.record(direction="lark→jira", event="updated", lark_id=rid,
                   jira_key=jira_key, description=desc)


def _handle_delete(rid: str, cfg: dict) -> None:
    if dedup.is_ours(f"lark:{rid}"):
        log.info(f"lark_handler: skipping delete {rid} — dedup (triggered by our own reconcile)")
        return

    jira_key = index._lark_to_jira.get(rid)
    if not jira_key:
        return

    # Verify the record is actually gone — Lark sometimes fires record_deleted spuriously.
    try:
        token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
        lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
        # Record still exists — spurious event, do nothing.
        log.warning(f"lark_handler: ignoring spurious record_deleted for {rid} ({jira_key}) — record still exists in Lark")
        return
    except Exception:
        pass  # Record is truly gone — proceed to unlink.

    # Lark record deletion does NOT cascade to Jira — just unlink from index.
    index.remove_by_jira(jira_key)
    log.info(f'lark_handler: Lark {rid} deleted — unlinked {jira_key}, Jira issue preserved')
    history.record(direction="lark→jira", event="deleted", lark_id=rid,
                   jira_key=jira_key,
                   description=f'Lark record deleted — {jira_key} preserved in Jira')
