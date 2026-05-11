"""Lark Base events → Jira actions."""
import time
import logging
import lark_api, jira_api, index, dedup
from config import (F_TITLE, F_START, F_END, F_ASSIGNEE, F_JIRA_KEY, F_JIRA_URL,
                    F_TYPE, F_PARENT, F_RELEASE, LARK_TO_JIRA_ASSIGNEE)
from utils import _lark_text, _lark_select, _lark_ts_to_jira_date, _norm

log = logging.getLogger(__name__)

_account_ids_cache: dict = {"data": None, "expires_at": 0}
_version_cache: dict = {"data": {}, "expires_at": 0}
_sprint_cache: dict = {"data": {}, "expires_at": 0}

ALLOWED_TYPES = {"Epic", "Story", "Task"}


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
    """Resolve Lark Parent items field → Jira key of the parent issue."""
    parent_data = rec["fields"].get(F_PARENT) or []
    for item in parent_data:
        if isinstance(item, dict):
            parent_rid = item.get("record_id") or item.get("id")
            if parent_rid:
                jk = index._lark_to_jira.get(parent_rid)
                if jk:
                    return jk
    return None


def process(action: dict, table_id: str, cfg: dict) -> None:
    act = action.get("action")
    rid = action.get("record_id", "")
    try:
        if act == "record_added":
            _handle_create(rid, table_id, cfg)
        elif act == "record_edited":
            _handle_update(rid, table_id, cfg)
        elif act == "record_deleted":
            _handle_delete(rid, cfg)
    except Exception as e:
        log.error(f"lark_handler.{act} rid={rid}: {e}")


def _handle_create(rid: str, table_id: str, cfg: dict) -> None:
    if dedup.is_ours(f"lark:{rid}"):
        return

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)

    if _lark_text(rec["fields"].get(F_JIRA_KEY)):
        return  # already linked — our write-back triggered this

    itype = _lark_select(rec["fields"].get(F_TYPE))
    if itype not in ALLOWED_TYPES:
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
    log.info(f"Created Jira {new_key} from Lark {rid}")


def _handle_update(rid: str, table_id: str, cfg: dict) -> None:
    if dedup.is_ours(f"lark:{rid}"):
        return

    jira_key = index._lark_to_jira.get(rid)
    if not jira_key:
        return  # not linked yet

    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)

    updates: dict = {}

    title = _lark_text(rec["fields"].get(F_TITLE))
    if title:
        updates["summary"] = title

    start = _lark_ts_to_jira_date(rec["fields"].get(F_START))
    if start:
        updates["customfield_10015"] = start

    end = _lark_ts_to_jira_date(rec["fields"].get(F_END))
    if end:
        updates["duedate"] = end

    assignee_lark = _lark_select(rec["fields"].get(F_ASSIGNEE))
    if assignee_lark:
        jira_name = LARK_TO_JIRA_ASSIGNEE.get(assignee_lark, "")
        account_id = _get_account_ids(cfg).get(jira_name)
        if account_id:
            updates["assignee"] = {"id": account_id}

    release_raw = (_lark_text(rec["fields"].get(F_RELEASE))
                   or _lark_select(rec["fields"].get(F_RELEASE)))
    if release_raw:
        vid = _get_version_map(cfg).get(_norm(release_raw))
        if vid:
            updates["fixVersions"] = [{"id": vid}]

    parent_jira_key = _resolve_parent(rec)
    if parent_jira_key:
        updates["parent"] = {"key": parent_jira_key}

    if not updates:
        return

    dedup.mark(f"jira:{jira_key}")
    jira_api.update_issue(cfg, jira_key, updates)

    if release_raw:
        sid = _get_sprint_map(cfg).get(_norm(release_raw))
        if sid:
            try:
                jira_api.move_to_sprint(cfg, sid, jira_key)
            except Exception as e:
                log.warning(f"Sprint move {jira_key}: {e}")

    log.info(f"Updated Jira {jira_key} from Lark {rid}")


def _handle_delete(rid: str, cfg: dict) -> None:
    jira_key = index._lark_to_jira.get(rid)
    if not jira_key:
        return
    dedup.mark(f"jira:{jira_key}")
    try:
        jira_api.delete_issue(cfg, jira_key)
    except Exception as e:
        log.error(f"Delete Jira {jira_key}: {e}")
    index.remove_by_jira(jira_key)
    log.info(f"Deleted Jira {jira_key} (Lark {rid} deleted)")
