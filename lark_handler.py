"""Lark Base events → Jira actions."""
import json
import time
import threading
import logging
import lark_api, jira_api, index, dedup, history, field_mappings, config
from config import (F_TITLE, F_START, F_END, F_ASSIGNEE, F_JIRA_KEY, F_JIRA_URL,
                    F_TYPE, F_PARENT, F_RELEASE, F_JIRA_STATUS,
                    LARK_TO_JIRA_ASSIGNEE)
from utils import _lark_text, _lark_select, _lark_ts_to_jira_date, _norm, _lark_link_rid

log = logging.getLogger(__name__)

_account_ids_cache: dict = {"data": None, "expires_at": 0}
_version_cache: dict = {"data": {}, "expires_at": 0}
_sprint_cache: dict = {"data": {}, "expires_at": 0}

# Serializes concurrent record_added handlers for the same rid. Lark sometimes
# delivers the same event multiple times in parallel; without this guard each
# delivery creates its own Jira issue.
_create_in_flight: set[str] = set()
_create_lock = threading.Lock()

# Coalesces concurrent record_edited handlers for the same rid. Lark frequently
# fires several record_edited events for one user edit (and the same event can
# be delivered twice). Without this guard each delivery races on get_record and
# the parallel calls trip Lark's per-Base QPS cap (429s in history). When a
# handler is already in flight, additional events just flag a re-run so the
# in-flight handler does one more pass with fresh state at the end.
_update_in_flight: set[str] = set()
_update_pending: set[str] = set()
_update_lock = threading.Lock()


def _get_account_ids(cfg: dict) -> dict:
    now = time.time()
    if _account_ids_cache["data"] and now < _account_ids_cache["expires_at"]:
        return _account_ids_cache["data"]
    data = jira_api.get_account_ids(cfg)
    _account_ids_cache.update({"data": data, "expires_at": now + 3600})
    return data


# Force a sprint/version map refresh at most once per this interval even on
# repeated misses, so a Release value that is a version-only name (never a
# sprint) can't trigger a Jira refetch on every single edit.
_FORCED_REFRESH_INTERVAL = 60


def _get_version_map(cfg: dict, force: bool = False) -> dict:
    now = time.time()
    if not force and _version_cache["data"] and now < _version_cache["expires_at"]:
        return _version_cache["data"]
    try:
        data = {_norm(v["name"]): v["id"] for v in jira_api.get_project_versions(cfg)}
    except Exception:
        data = {}
    _version_cache.update({"data": data, "expires_at": now + 3600})
    return data


def _get_sprint_map(cfg: dict, force: bool = False) -> dict:
    now = time.time()
    if not force and _sprint_cache["data"] and now < _sprint_cache["expires_at"]:
        return _sprint_cache["data"]
    try:
        bid = jira_api.get_board_id(cfg)
        data = ({_norm(s["name"]): s["id"] for s in jira_api.get_board_sprints(cfg, bid)}
                if bid else {})
    except Exception:
        data = {}
    _sprint_cache.update({"data": data, "expires_at": now + 3600})
    return data


def _resolve_id(cache: dict, getter, cfg: dict, name: str):
    """Resolve a normalized Release name → Jira id, refreshing once on miss.

    A Jira sprint/version created within the map's 1 h TTL isn't in the
    cached map, so the first lookup misses and the Release→sprint /
    Release→fixVersion sync is silently skipped (no error). On miss, force a
    single refresh before giving up. Throttled via cache['last_forced'] so a
    version-only Release name can't cause a Jira refetch on every edit.
    """
    val = getter(cfg).get(name)
    if val is not None:
        return val
    if time.time() - cache.get("last_forced", 0) < _FORCED_REFRESH_INTERVAL:
        return None
    cache["last_forced"] = time.time()
    return getter(cfg, force=True).get(name)


def _resolve_parent(rec: dict) -> "str | None":
    parent_data = rec["fields"].get(F_PARENT) or []
    for item in parent_data:
        rid = _lark_link_rid([item])
        if rid:
            jk = index._lark_to_jira.get(rid)
            if jk:
                return jk
    return None


# Lark Bitable field type numbers (verified against the live Base schema).
_FT_TEXT, _FT_NUMBER, _FT_SINGLE_SELECT = 1, 2, 3
_FT_MULTI_SELECT, _FT_DATETIME, _FT_LINK = 4, 5, 18


def _relevant_lark_fields() -> set:
    """Lark field names the Lark→Jira update path actually reads.

    Anything not in this set (e.g. a recomputed formula field that Lark
    bundles into the same webhook) is ignored during decode so it doesn't
    needlessly force a get_record fallback.

    `F_JIRA_STATUS` is included only when its configured direction covers
    Lark→Jira (`both` or `lark_to_jira`). The default is `jira_to_lark` — for
    users who didn't opt in, decoding the status from the webhook would be
    wasted work (the value-compare in the update branch would still skip
    the write, but it would force a get_record on every Jira-status webhook).
    """
    names = {F_TITLE, F_START, F_END, F_ASSIGNEE, F_RELEASE, F_PARENT}
    if field_mappings.get_direction(F_JIRA_STATUS) in ("both", "lark_to_jira"):
        names.add(F_JIRA_STATUS)
    names.update(m["lark_field"] for m in field_mappings.get_custom_lark_to_jira())
    return names


def _decode_one(ftype, raw, options):
    """Decode one webhook field_value into the shape get_record returns.

    Returns (value, ok). ok=False means "can't safely decode — caller should
    fall back to get_record". An empty/cleared value decodes to (None, True);
    the update handler already skips None-valued fields.
    """
    if raw is None or raw == "":
        return None, True
    if ftype == _FT_TEXT:
        # The webhook delivers rich-text/url fields as a JSON-STRINGIFIED
        # segment array, e.g. '[{"text":"hi","type":"text"}]'. Returning that
        # string as-is made _lark_text yield the literal JSON instead of "hi",
        # which got written to Jira summary and re-synced back, nesting one
        # more layer each round (the VR-227 Title corruption loop). Parse it
        # to the list shape get_record returns so _lark_text extracts the
        # text. Genuinely-plain values are kept as-is.
        if isinstance(raw, str) and raw[:1] == "[":
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return None, False  # undecodable → fall back to get_record
            if isinstance(parsed, list):
                return parsed, True
            return None, False
        return raw, True
    if ftype == _FT_NUMBER:
        return raw, True  # custom mappings coerce via float(raw)
    if ftype == _FT_SINGLE_SELECT:
        name = options.get(raw)
        return (name, True) if name is not None else (None, False)
    if ftype == _FT_MULTI_SELECT:
        try:
            ids = json.loads(raw)
        except (ValueError, TypeError):
            return None, False
        if not isinstance(ids, list):
            return None, False
        names = []
        for oid in ids:
            nm = options.get(oid)
            if nm is None:
                return None, False
            names.append(nm)
        return names, True  # matches get_record shape: ["Nurse", ...]
    if ftype == _FT_DATETIME:
        try:
            return int(raw), True
        except (ValueError, TypeError):
            return None, False
    if ftype == _FT_LINK:
        try:
            rids = json.loads(raw)
        except (ValueError, TypeError):
            return None, False
        if not isinstance(rids, list):
            return None, False
        return [{"record_ids": rids}], True  # matches _lark_link_rid expectation
    return None, False  # unhandled type in sync scope → force safe fallback


def _decode_after_value(after_value, token, cfg, before_value=None, record_id=None):
    """Translate webhook [{field_id, field_value}] → {field_name: value}.

    Lark sends a *full* record snapshot in both before_value and after_value
    on every record_edited event — only a few field_values actually differ.
    When `before_value` is supplied, only fields whose raw value differs are
    decoded. Without this filter, decoded ends up populated with every
    relevant field's current value on every webhook, and downstream handlers
    (especially the custom-mapping loop) re-push them to Jira — the
    2026-05 reconcile→QA-Manday echo burst.

    Value shapes match what lark_api.get_record returns, so the rest of
    _handle_update_impl runs unchanged. Returns None if any *relevant*
    changed field can't be confidently decoded (unknown field, unknown
    select option, bad encoding) — caller then falls back to get_record.
    """
    try:
        meta = lark_api.get_field_meta_by_id(
            token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
    except Exception as e:
        log.info(f"lark_handler: field meta fetch failed ({e}) — fall back to get_record")
        return None

    before_by_fid = {b.get("field_id"): b.get("field_value")
                     for b in (before_value or [])}
    have_before = bool(before_value)

    relevant = _relevant_lark_fields()
    fields = {}
    for av in after_value:
        fid = av.get("field_id")
        m = meta.get(fid)
        if not m:
            log.info(f"lark_handler: unknown field_id {fid} in webhook — fall back to get_record")
            return None
        fname = m["name"]
        if fname not in relevant:
            continue  # irrelevant field (e.g. formula recompute) — ignore, don't fall back
        # Skip fields whose raw value did not change in this webhook. Lark
        # repeats every field's value in after_value regardless of whether it
        # changed, so without this gate "Release changed" implicitly carries
        # "P. QA md is 1.0" and re-pushes QA Manday to Jira.
        if have_before and av.get("field_value") == before_by_fid.get(fid):
            continue
        value, ok = _decode_one(m["type"], av.get("field_value"), m["options"])
        if not ok:
            log.info(f"lark_handler: can't decode '{fname}' (type {m['type']}) — fall back to get_record")
            return None
        fields[fname] = value
    # Populate the value cache so a subsequent Jira→Lark webhook for this
    # same record reads fresh values without a get_record fetch. Skipped
    # when no record_id was supplied (legacy callers) or when no relevant
    # fields changed (nothing to merge).
    if record_id is not None and fields:
        lark_api._cache_merge(record_id, fields)
    return fields


def process(action: dict, table_id: str, cfg: dict) -> None:
    act = action.get("action")
    rid = action.get("record_id", "")
    log.info(f"lark_handler: action={act} record_id={rid} table_id={table_id}")
    try:
        if act == "record_added":
            _handle_create(rid, table_id, cfg)
        elif act == "record_edited":
            _handle_update(rid, table_id, cfg,
                           after_value=action.get("after_value") or [],
                           before_value=action.get("before_value") or [])
        elif act == "record_deleted":
            _handle_delete(rid, cfg)
        else:
            log.warning(f"lark_handler: unknown action '{act}' for {rid}")
    except Exception as e:
        log.error(f"lark_handler.{act} rid={rid}: {e}", exc_info=True)
        jira_key = index._lark_to_jira.get(rid, "")
        # Don't re-call Lark to recover Type when the failure was itself a Lark
        # API error — that just adds more load to an already rate-limited API.
        itype = ""
        if "larksuite.com" not in str(e):
            try:
                token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
                rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
                itype = _lark_select(rec["fields"].get(F_TYPE)) or ""
            except Exception:
                pass
        history.record(direction="lark→jira", event=act or "unknown",
                       lark_id=rid, jira_key=jira_key,
                       description=f"Lark update error: {e}",
                       status="error", error=str(e), type=itype)


def _handle_create(rid: str, table_id: str, cfg: dict) -> None:
    # Atomically claim this rid. If another handler is already creating for the
    # same record, skip — Lark delivers duplicate record_added events in parallel.
    with _create_lock:
        if rid in _create_in_flight:
            log.info(f"lark_handler: skipping create {rid} — another handler already in flight")
            return
        _create_in_flight.add(rid)
    try:
        if dedup.is_ours(f"lark:{rid}"):
            log.info(f"lark_handler: skipping create {rid} — dedup")
            return

        token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
        rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
        log.info(f"lark_handler: record fields keys={list(rec['fields'].keys())}")

        existing_key = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if existing_key:
            log.info(f"lark_handler: skipping create {rid} — Jira Key already set ({existing_key})")
            index.add(existing_key, rid)  # backfill the index so future edits sync
            return

        itype = _lark_select(rec["fields"].get(F_TYPE))
        allowed = config.get_allowed_lark_types()
        if not itype:
            log.info(f"lark_handler: deferring create {rid} — Type not set yet")
            return  # silent: user is mid-typing, will retry on next record_edited
        if itype not in allowed:
            log.info(f"lark_handler: skipping create {rid} — type '{itype}' not in {allowed}")
            history.record(direction="lark→jira", event="created", lark_id=rid,
                           description=f"Skipped: type '{itype}' not in allowed types",
                           status="skipped", type=itype)
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
        # Mark + link BEFORE updating Lark, so the Jira webhook firing back for
        # this issue is recognized as our own creation even if it arrives before
        # the lark_api.update_record call below completes.
        dedup.mark(f"jira:{new_key}")
        dedup.mark(f"lark:{rid}")
        index.add(new_key, rid)

        lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid, {
            F_JIRA_KEY: new_key,
            F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{new_key}",
        })
        log.info(f"lark_handler: created Jira {new_key} from Lark {rid}")
        history.record(direction="lark→jira", event="created", lark_id=rid,
                       jira_key=new_key, description=f"Created {itype}: \"{title}\"",
                       type=itype)
    finally:
        with _create_lock:
            _create_in_flight.discard(rid)


def _handle_update(rid: str, table_id: str, cfg: dict,
                   after_value=None, before_value=None) -> None:
    # Coalesce duplicate / rapid-fire record_edited events for the same rid into
    # at most two sequential passes (initial + re-run with fresh state if more
    # events arrived during processing). Parallel get_records on the same rid
    # were the main source of 429s in the history log.
    #
    # The first pass may use the webhook's after_value payload (fast path, no
    # get_record). The coalesced re-run intentionally passes after_value=None so
    # it re-reads fresh full state via get_record — the coalesced events that
    # triggered the re-run carried their own (now-discarded) payloads.
    with _update_lock:
        if rid in _update_in_flight:
            _update_pending.add(rid)
            log.info(f"lark_handler: coalescing update {rid} — handler already in flight")
            return
        _update_in_flight.add(rid)
    try:
        _handle_update_impl(rid, table_id, cfg,
                            after_value=after_value, before_value=before_value)
        while True:
            with _update_lock:
                if rid not in _update_pending:
                    break
                _update_pending.discard(rid)
            _handle_update_impl(rid, table_id, cfg,
                                after_value=None, before_value=None)
    finally:
        with _update_lock:
            _update_in_flight.discard(rid)
            _update_pending.discard(rid)


def _handle_update_impl(rid: str, table_id: str, cfg: dict,
                        after_value=None, before_value=None) -> None:
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])

    jira_key = index._lark_to_jira.get(rid)
    rec = None

    # Fast path: we already know the Jira link AND the webhook handed us the
    # changed fields → decode the payload and skip get_record entirely. This is
    # the single biggest Lark API-call saving on the webhook hot path.
    if jira_key and after_value:
        decoded = _decode_after_value(after_value, token, cfg,
                                      before_value=before_value, record_id=rid)
        if decoded is not None:
            if not decoded:
                log.info(f"lark_handler: {rid} no synced fields changed — skipping (no get_record)")
                return
            rec = {"record_id": rid, "fields": decoded}
            log.info(
                f"lark_handler: {rid} fast-path update — {len(decoded)} field(s) "
                f"from webhook, no get_record")

    if rec is None:
        rec = lark_api.get_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)

    if not jira_key:
        # Not in index — check if the record itself has a Jira Key (auto-discover)
        jira_key = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if jira_key:
            index.add(jira_key, rid)
            log.info(f"lark_handler: auto-discovered link {rid} → {jira_key}")
        else:
            # No link yet — happens when record_added fired before Type was set
            # (skipped silently) or when Lark only emits record_edited. Route to
            # create so the record isn't stranded once it has a valid Type.
            log.info(f"lark_handler: {rid} not linked — routing to create handler")
            _handle_create(rid, table_id, cfg)
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
    if (start and start != jira_fields.get("customfield_10015")
            and not dedup.is_ours(dedup.date_echo_key(jira_key, "start", start))):
        updates["customfield_10015"] = start
        changed.append(f"Start: {start}")

    end = _lark_ts_to_jira_date(rec["fields"].get(F_END))
    if (end and end != jira_fields.get("duedate")
            and not dedup.is_ours(dedup.date_echo_key(jira_key, "end", end))):
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
        vid = _resolve_id(_version_cache, _get_version_map, cfg, _norm(release_raw))
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

    # Jira status (Lark → Jira). Honored only when the field's configured
    # direction covers Lark→Jira (default 'jira_to_lark' is skipped — a
    # Lark→Jira status push against a user who didn't opt in would silently
    # transition Jira issues). The transition itself is done via the workflow
    # API (not update_issue — Jira rejects status writes outside the workflow),
    # so we track the target in `status_change_target` and fire it after the
    # normal update_issue / move_to_sprint path below. Value-compare against
    # current Jira status: same value → skip, no Jira call.
    status_change_target = None
    if field_mappings.get_direction(F_JIRA_STATUS) in ("both", "lark_to_jira"):
        new_status = _lark_text(rec["fields"].get(F_JIRA_STATUS))
        cur_status = (jira_fields.get("status") or {}).get("name") or ""
        if new_status and new_status != cur_status:
            status_change_target = new_status

    # Apply custom (non-system) Lark → Jira mappings.
    # Value-compare each one against current Jira state before adding to updates.
    # System fields (Title, dates, etc.) already do this; custom mappings used
    # to write unconditionally — so every reconcile-triggered webhook re-pushed
    # the same custom values to Jira (the 2026-05 QA-Manday echo burst, layered
    # on top of the decoder fix as defense in depth).
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
        if not val:
            continue
        cur = jira_fields.get(m["jira_field"])
        if ft == "number":
            cur_num = float(cur) if isinstance(cur, (int, float)) else None
            if val == cur_num:
                continue
        elif ft == "date":
            if val == cur:  # both canonical "YYYY-MM-DD"
                continue
        else:
            if isinstance(cur, dict):
                cur = cur.get("value") or cur.get("name")
            if val == cur:
                continue
        updates[m["jira_field"]] = val
        changed.append(f"{m['jira_label'] or m['lark_field']}: {val}")

    # Resolve the Release→sprint move BEFORE the early-return guard. A Release
    # change that maps to a sprint but not a fixVersion (or whose fixVersion
    # already matches) produces no `updates`; gating the sprint move behind
    # `if not updates` silently skipped it — the second half of the
    # "new sprint doesn't sync" bug.
    sid = None
    if release_raw:
        sid = _resolve_id(_sprint_cache, _get_sprint_map, cfg, _norm(release_raw))
        if sid:
            current_sprint_ids = [s.get("id") for s in
                                  (jira_fields.get("customfield_10020") or [])]
            if sid in current_sprint_ids:
                sid = None  # already in that sprint — skip the redundant Jira call

    if not updates and not sid and not status_change_target:
        log.info(f"lark_handler: no relevant updates for {jira_key}")
        return

    if updates:
        log.info(f"lark_handler: sending to Jira {jira_key} fields={list(updates.keys())}")
        jira_api.update_issue(cfg, jira_key, updates)
        # Echo-suppress: the Jira write fires a Jira→Lark webhook for these
        # exact dates; mark them so jira_handler skips re-propagating (breaks
        # the bidirectional conflict ping-pong). Symmetric with jira_handler.
        if "customfield_10015" in updates:
            dedup.mark(dedup.date_echo_key(jira_key, "start", updates["customfield_10015"]))
        if "duedate" in updates:
            dedup.mark(dedup.date_echo_key(jira_key, "end", updates["duedate"]))

    if sid:
        try:
            jira_api.move_to_sprint(cfg, sid, jira_key)
            changed.append(f"Sprint: {release_raw}")
        except Exception as e:
            log.warning(f"Sprint move {jira_key}: {e}")

    if status_change_target:
        # Fire after update_issue/move_to_sprint so a "Jira status push" bug
        # doesn't blow up a successful normal-field write. `False` return =
        # workflow has no transition to the target (e.g. Done → To Do) — log
        # and skip, never force a path the workflow forbids.
        try:
            if jira_api.transition_issue(cfg, jira_key, status_change_target):
                changed.append(f"Status: {status_change_target}")
            else:
                log.warning(
                    f"lark_handler: no Jira transition for {jira_key} → "
                    f"{status_change_target} (workflow forbids it; not pushing)")
        except Exception as e:
            log.warning(
                f"lark_handler: Jira transition {jira_key} → "
                f"{status_change_target} failed: {e}")

    desc = ", ".join(changed)
    log.info(f"lark_handler: updated Jira {jira_key} — {desc}")
    itype_now = _lark_select(rec["fields"].get(F_TYPE)) or ""
    history.record(direction="lark→jira", event="updated", lark_id=rid,
                   jira_key=jira_key, description=desc, type=itype_now)


def _handle_delete(rid: str, cfg: dict) -> None:
    # `lark_delete:{rid}` is marked only when our code deletes a Lark record
    # (jira_handler cascade, reconcile cleanup). We must NOT use `lark:{rid}`
    # here — that key is also marked when we write/update a record, which would
    # cause user-initiated deletes within 120 s of a write to be silently dropped.
    if dedup.is_ours(f"lark_delete:{rid}"):
        log.info(f"lark_handler: skipping delete {rid} — dedup (delete originated from our code)")
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
        pass  # Record is truly gone — proceed.

    # Type isn't recoverable from Lark anymore — pull from Jira's issuetype before deleting.
    itype = ""
    try:
        itype = ((jira_api.get_issue(cfg, jira_key) or {}).get("fields", {})
                 .get("issuetype", {}) or {}).get("name", "")
    except Exception:
        pass

    # Cascade delete to Jira. Mark dedup first so the jira:issue_deleted webhook
    # firing back for this delete is recognized as our own.
    dedup.mark(f"jira:{jira_key}")
    try:
        jira_api.delete_issue(cfg, jira_key)
    except Exception as e:
        log.error(f"lark_handler: failed to delete Jira {jira_key}: {e}")
        history.record(direction="lark→jira", event="deleted", lark_id=rid,
                       jira_key=jira_key,
                       description=f"Failed to delete Jira {jira_key}: {e}",
                       status="error", error=str(e), type=itype)
        return

    index.remove_by_jira(jira_key)
    log.info(f"lark_handler: Lark {rid} deleted — cascaded delete to Jira {jira_key}")
    history.record(direction="lark→jira", event="deleted", lark_id=rid,
                   jira_key=jira_key,
                   description=f"Deleted Jira {jira_key} (Lark record deleted)",
                   type=itype)
