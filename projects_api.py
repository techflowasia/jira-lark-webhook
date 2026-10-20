"""Project-scoped settings API and setup checks.

Every `/api/p/{pid}/...` handler runs its work inside `projects.use(pid)`
(unknown pid -> 404). The unscoped legacy endpoints in main.py are thin
aliases that call the same `do_*` functions for project #1.

Nothing here writes a Jira or Lark RECORD except the explicit backfill
endpoint; the setup/check endpoints only read schemas.
"""
import asyncio
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import config
import field_mappings
import history
import index
import jira_api
import lark_api
import projects
import reconcile
from utils import _lark_text

log = logging.getLogger(__name__)
router = APIRouter()

SETUP_STATES = ("draft", "active")
_PATCHABLE_TYPES = ("allowed_jira_types", "allowed_lark_types")


# ── helpers ─────────────────────────────────────────────────────────────────

def _project(pid: int) -> "projects.Project":
    p = projects.get(pid)
    if p is None:
        raise HTTPException(404, f"project {pid} not found")
    return p


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _conflict_or_bad(e: ValueError) -> JSONResponse:
    return _err(409 if "already synced" in str(e) else 400, str(e))


def _lark_token(cfg: dict) -> str:
    return lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])


def project_dict(p: "projects.Project") -> dict:
    return asdict(p)


def _valid_str_map(v) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())


def _valid_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _rebuild_now(pid: int) -> None:
    """Rebuild one project's index synchronously. Raises on any failure."""
    p = projects.get(pid)
    if p is None:
        raise KeyError(f"project {pid} not found")
    cfg = projects.cfg_for(p)
    token = _lark_token(cfg)
    records = lark_api.fetch_all_records(token, p.lark_base_token, p.lark_table_id)
    with projects.use(pid):
        index.rebuild(records)
        log.info(f"Project {pid}: table '{p.lark_table_name}' ({p.lark_table_id}), "
                 f"{len(index._jira_to_lark)} records indexed")
        history.record(direction="system", event="config",
                       description=f"Index rebuilt for Lark table '{p.lark_table_name}' ({p.lark_table_id})")


def _rebuild_sync(pid: int) -> None:
    try:
        _rebuild_now(pid)
    except Exception as e:
        log.error(f"Index rebuild failed for project {pid}: {e}")


def _start_rebuild(pid: int) -> None:
    """Rebuild one project's index in the background (inside projects.use)."""
    threading.Thread(target=_rebuild_sync, args=(pid,), daemon=True).start()


def _on_table_changed() -> None:
    # Cached schemas / field values of the previous table aren't portable.
    lark_api.invalidate_fields_cache()
    lark_api.invalidate_record_cache()


# ── do_* functions: run inside projects.use(pid); shared with legacy aliases ─

def do_tables(pid: int) -> dict:
    with projects.use(pid):
        cfg = config.get_cfg()
        try:
            tables = lark_api.list_tables(_lark_token(cfg), cfg["LARK_BASE_TOKEN"])
            return {"tables": tables, "active_table_id": cfg["LARK_TABLE_ID"]}
        except Exception as e:
            return {"error": str(e), "tables": []}


def do_lark_fields(pid: int) -> dict:
    with projects.use(pid):
        cfg = config.get_cfg()
        try:
            return {"fields": lark_api.list_fields(
                _lark_token(cfg), cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])}
        except Exception as e:
            return {"fields": [], "error": str(e)}


def do_jira_fields(pid: int) -> dict:
    with projects.use(pid):
        try:
            fields = jira_api.get_all_fields(config.get_cfg())
            fields.sort(key=lambda f: (f["custom"], f["name"].lower()))
            return {"fields": fields}
        except Exception as e:
            return {"fields": [], "error": str(e)}


def do_jira_issue_types(pid: int) -> dict:
    with projects.use(pid):
        try:
            return {"types": jira_api.get_project_issue_types(config.get_cfg())}
        except Exception as e:
            return {"types": [], "error": str(e)}


def do_lark_type_options(pid: int) -> dict:
    with projects.use(pid):
        cfg = config.get_cfg()
        try:
            return {"options": lark_api.get_select_options(
                _lark_token(cfg), cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"],
                field_mappings.F_TYPE)}
        except Exception as e:
            return {"options": [], "error": str(e)}


def do_fields(pid: int) -> dict:
    with projects.use(pid):
        return {"mappings": field_mappings.get_all()}


def do_save_field(pid: int, body: dict) -> dict:
    with projects.use(pid):
        try:
            return {"ok": True, "mapping": field_mappings.upsert(body)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def do_delete_field(pid: int, mapping_id: int) -> dict:
    with projects.use(pid):
        try:
            field_mappings.delete(mapping_id)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def do_sync_types(pid: int) -> dict:
    with projects.use(pid):
        return {"allowed_jira": sorted(config.get_allowed_jira_types()),
                "allowed_lark": sorted(config.get_allowed_lark_types())}


def do_save_sync_types(pid: int, body: dict) -> dict:
    with projects.use(pid):
        try:
            config.save_sync_types(body.get("jira_types", []), body.get("lark_types", []))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def do_backfill(pid: int) -> dict:
    empty = {"removed_duplicates": 0, "matched": 0, "skipped_ambiguous": 0,
             "created_lark": 0, "created_jira": 0, "synced": 0, "pairs": []}
    with projects.use(pid):
        # With a required mapping missing every field would be skipped and a
        # backfill would create blank records — never run in that state.
        missing = field_mappings.missing_required()
        if missing:
            return {**empty, "error": "required mapping missing: " + ", ".join(missing)}
        try:
            return reconcile.backfill(config.get_cfg())
        except Exception as e:
            return {**empty, "error": str(e)}


def do_set_table(pid: int, table_id: str, name: str = "") -> dict:
    """Point a project at another table of its Base. Raises ValueError (e.g.
    the table is already synced by another project)."""
    table_id, name = (table_id or "").strip(), (name or "").strip()
    if not table_id:
        raise ValueError("table_id required")
    projects.update(pid, {"lark_table_id": table_id, "lark_table_name": name})
    _on_table_changed()
    _start_rebuild(pid)
    return {"ok": True, "table_id": table_id, "name": name, "rebuilding": True}


def do_patch(pid: int, body: dict) -> "projects.Project":
    """Validate and apply the patchable settings. Base token and Jira key are
    deliberately not patchable. Raises ValueError (HTTP 400/409)."""
    changes: dict = {}
    if "name" in body:
        if not isinstance(body["name"], str) or not body["name"].strip():
            raise ValueError("name must be a non-empty string")
        changes["name"] = body["name"].strip()
    for k in ("jira_board_id", "board_id", "board"):
        if k in body:
            v = body[k]
            changes["jira_board_id"] = None if v in (None, "") else str(v)
    if "assignee_map" in body:
        if not _valid_str_map(body["assignee_map"]):
            raise ValueError("assignee_map must be a JSON object of string to string")
        changes["assignee_map"] = dict(body["assignee_map"])
    for k in _PATCHABLE_TYPES:
        if k in body:
            if not _valid_str_list(body[k]):
                raise ValueError(f"{k} must be a list of strings")
            changes[k] = list(body[k])
    if "setup_state" in body:
        if body["setup_state"] not in SETUP_STATES:
            raise ValueError(f"setup_state must be one of {', '.join(SETUP_STATES)}")
        changes["setup_state"] = body["setup_state"]
    for k in ("sync_enabled", "reconcile_enabled"):
        if k in body:
            if not isinstance(body[k], bool):
                raise ValueError(f"{k} must be true or false")
            changes[k] = body[k]
    before = projects.get(pid)
    going_live = changes.get("setup_state") == "active" and before.setup_state != "active"
    if (changes.get("sync_enabled") or changes.get("reconcile_enabled")) \
            and changes.get("setup_state", before.setup_state) != "active":
        raise ValueError("finish setup (setup_state=active) before turning sync or reconcile on")
    table_changed = False
    if body.get("lark_table_id"):
        cur = projects.get(pid)
        changes["lark_table_id"] = str(body["lark_table_id"]).strip()
        changes["lark_table_name"] = str(body.get("lark_table_name", "")).strip()
        table_changed = changes["lark_table_id"] != cur.lark_table_id
    if going_live:
        if table_changed:
            # Apply the new table first so the check and the index rebuild
            # below use it (the project is still draft with sync off).
            projects.update(pid, {k: changes.pop(k) for k in ("lark_table_id", "lark_table_name")})
            _on_table_changed()
            table_changed = False
        with projects.use(pid):
            missing = field_mappings.missing_required()
        if missing:
            raise ValueError("required mapping missing: " + ", ".join(missing))
        # Server-side gate for wizard step 5: every mapping must check out.
        chk = do_check(pid)
        if not chk["ready"]:
            raise ValueError("field check failed: " + "; ".join(
                pr["problem"] for pr in chk["fields"]))
        # The index must exist before sync turns on. Build it now; on failure
        # nothing else is changed and the project stays draft with sync off.
        try:
            _rebuild_now(pid)
        except Exception as e:
            log.error(f"Go-live index rebuild failed for project {pid}: {e}", exc_info=True)
            raise ValueError("could not build the index (see server log); "
                             "project left as draft")
    p = projects.update(pid, changes)  # raises ValueError on duplicate table
    if table_changed:
        _on_table_changed()
        _start_rebuild(pid)
    return p


def do_delete(pid: int) -> dict:
    """Delete a draft project (registry row + its field mappings only).
    Raises ValueError (HTTP 400)."""
    projects.delete(pid)
    return {"ok": True}


def do_seed_defaults(pid: int) -> dict:
    return {"ok": True, "seeded": field_mappings.seed_defaults(pid)}


def do_copy(pid: int, src_id: int) -> dict:
    """Copy mappings, sync types and people from project src_id into the
    (still empty) project pid. Raises HTTPException."""
    src = projects.get(src_id)
    if src is None:
        raise HTTPException(404, f"copy source project {src_id} not found")
    if src_id == pid:
        raise HTTPException(400, "cannot copy a project from itself")
    try:
        n = field_mappings.copy_from(src_id, pid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    p = projects.update(pid, {"allowed_jira_types": list(src.allowed_jira_types),
                              "allowed_lark_types": list(src.allowed_lark_types),
                              "assignee_map": dict(src.assignee_map)})
    return {**project_dict(p), "copied_mappings": n}


def do_resolve_link(link: str) -> dict:
    """Read-only: Base token + table from a pasted link, and the Base's tables."""
    cfg = projects.cfg_for(projects.get(min(p.id for p in projects.all_projects())))
    token = _lark_token(cfg)
    base, table = lark_api.parse_base_link(token, link)
    out = {"base_token": base, "table_id": table, "tables": []}
    try:
        out["tables"] = lark_api.list_tables(token, base)
    except Exception as e:  # the bot may not have access yet (Connect Lark comes later)
        out["tables_error"] = str(e)
    return out


def do_people_suggestions(pid: int) -> dict:
    """Read-only suggestions for the People step."""
    with projects.use(pid):
        cfg = config.get_cfg()
        out = {"jira_users": [], "lark_options": []}
        try:
            out["jira_users"] = sorted(jira_api.get_account_ids(cfg))
        except Exception as e:
            out["jira_error"] = str(e)
        f_assignee = field_mappings.F_ASSIGNEE
        if f_assignee:
            try:
                out["lark_options"] = lark_api.get_select_options(
                    _lark_token(cfg), cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], f_assignee)
            except Exception as e:
                out["lark_error"] = str(e)
        return out


def do_check(pid: int) -> dict:
    """Read-only setup check. Field problems: Lark column missing, Jira custom
    field unknown, required mapping missing. `ready` = no field problems."""
    with projects.use(pid):
        cfg = config.get_cfg()
        problems: list = []
        rows = [m for m in field_mappings.get_all() if m.get("active") is not False]

        lark_cols, token = None, None
        try:
            token = _lark_token(cfg)
            lark_cols = {f["field_name"] for f in lark_api.list_fields(
                token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])}
        except Exception as e:
            problems.append({"mapping_id": None, "lark_field": None, "jira_field": None,
                             "problem": f"could not read the Lark table: {e}"})
        jira_ids = None
        try:
            jira_ids = {f["id"] for f in jira_api.get_all_fields(cfg)}
        except Exception as e:
            problems.append({"mapping_id": None, "lark_field": None, "jira_field": None,
                             "problem": f"could not read Jira fields: {e}"})

        for m in rows:
            lf, jf = m.get("lark_field"), m.get("jira_field")
            if lark_cols is not None and lf not in lark_cols:
                problems.append({"mapping_id": m.get("id"), "lark_field": lf, "jira_field": jf,
                                 "problem": f"Lark column '{lf}' not found in the table"})
            if jira_ids is not None and (jf or "").startswith("customfield_") and jf not in jira_ids:
                problems.append({"mapping_id": m.get("id"), "lark_field": lf, "jira_field": jf,
                                 "problem": f"Jira field '{jf}' not found"})
        for jf in field_mappings.missing_required():
            problems.append({"mapping_id": None, "lark_field": None, "jira_field": jf,
                             "problem": f"required mapping missing for Jira field '{jf}'"})

        # Rows whose Jira Key names another Jira project (a Base duplicated
        # with its data): sync would never touch them, but they block go-live
        # so the user clears/fixes them knowingly.
        foreign = {"count": 0, "examples": []}
        f_key = field_mappings.F_JIRA_KEY
        if token and f_key and lark_cols is not None and f_key in lark_cols:
            try:
                records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"],
                                                     cfg["LARK_TABLE_ID"])
                bad = [k for k in (_lark_text(r["fields"].get(f_key)) for r in records)
                       if k and not projects.owns_key(k)]
                foreign = {"count": len(bad), "examples": sorted(set(bad))[:5]}
            except Exception as e:
                problems.append({"mapping_id": None, "lark_field": None, "jira_field": None,
                                 "problem": f"could not read the Lark records: {e}"})
        if foreign["count"]:
            p = projects.current()
            problems.append({
                "mapping_id": None, "lark_field": f_key, "jira_field": None,
                "problem": (f"{foreign['count']} Lark row(s) have a '{f_key}' of another Jira "
                            f"project (e.g. {', '.join(foreign['examples'])}), not "
                            f"{p.jira_project_key}. Sync ignores them; clear or fix them in "
                            f"Lark before going live")})

        people = {"unmapped_jira": [], "unknown_lark": []}
        amap = config.jira_to_lark_assignee()
        out = {"fields": problems, "people": people, "ready": not problems,
               "foreign_keys": foreign}
        try:
            accounts = jira_api.get_account_ids(cfg)
            people["unmapped_jira"] = sorted(n for n in accounts if n not in amap)
        except Exception as e:
            out["people_error"] = f"could not read Jira assignees: {e}"
        f_assignee = field_mappings.F_ASSIGNEE
        if token and f_assignee and lark_cols is not None:
            try:
                options = set(lark_api.get_select_options(
                    token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], f_assignee))
                # A user-type column has no options to compare against.
                if options:
                    people["unknown_lark"] = sorted({v for v in amap.values() if v not in options})
            except Exception as e:
                out["people_error"] = f"could not read Lark assignee options: {e}"
        return out


def do_list_projects() -> list:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    out = []
    for p in projects.all_projects():
        if p.setup_state == "draft":
            health = "draft"
        elif not p.sync_enabled:
            health = "paused"
        elif history.error_count(p.id, since) > 0:
            health = "error"
        else:
            health = "ok"
        out.append({"id": p.id, "name": p.name, "jira_project_key": p.jira_project_key,
                    "setup_state": p.setup_state, "sync_enabled": p.sync_enabled,
                    "reconcile_enabled": p.reconcile_enabled, "health": health})
    return out


def do_create(body: dict) -> dict:
    """Create a draft project (sync and reconcile off). Raises HTTPException."""
    key = (body.get("jira_project_key") or "").strip().upper()
    if not key:
        raise HTTPException(400, "jira_project_key is required")
    if not projects.valid_jira_key(key):
        raise HTTPException(400, "jira_project_key must be a Jira project key "
                                 "(letters, digits, underscore; starting with a letter)")
    src = None
    if body.get("copy_from") is not None:
        src = projects.get(int(body["copy_from"]))
        if src is None:
            raise HTTPException(404, f"copy_from project {body['copy_from']} not found")
    base, table = (body.get("lark_base_token") or "").strip(), (body.get("lark_table_id") or "").strip() or None
    cfg = projects.cfg_for(projects.get(min(p.id for p in projects.all_projects())))
    try:
        exists = jira_api.get_project({**cfg, "JIRA_PROJECT": key}, key)
    except Exception as e:
        log.warning(f"create project: Jira lookup of {key} failed: {e}")
        raise HTTPException(502, "Jira lookup failed (see server log)")
    if exists is None:
        raise HTTPException(400, f"Jira project {key} does not exist (or is not visible "
                                 "to the sync account)")
    try:
        if body.get("lark_base_link"):
            base, link_table = lark_api.parse_base_link(_lark_token(cfg), body["lark_base_link"])
            table = table or link_table
        if not base:
            raise ValueError("lark_base_link or lark_base_token is required")
        if not table:
            tables = lark_api.list_tables(_lark_token(cfg), base)
            if len(tables) != 1:
                raise ValueError("lark_table_id is required (the Base has "
                                 f"{len(tables)} tables and the link does not name one)")
            table = tables[0]["table_id"]
        fields = {"jira_project_key": key, "lark_base_token": base, "lark_table_id": table}
        if body.get("name"):
            fields["name"] = str(body["name"]).strip()
        p = projects.create(fields)
    except ValueError as e:
        raise HTTPException(409 if "already synced" in str(e) else 400, str(e))
    out = project_dict(p)
    if src is not None:
        try:
            out["copied_mappings"] = field_mappings.copy_from(src.id, p.id)
            p = projects.update(p.id, {"allowed_jira_types": list(src.allowed_jira_types),
                                       "allowed_lark_types": list(src.allowed_lark_types),
                                       "assignee_map": dict(src.assignee_map)})
            out = {**project_dict(p), "copied_mappings": out["copied_mappings"]}
        except Exception as e:
            # The project exists as a draft with no mappings: it cannot sync.
            out["copy_error"] = str(e)
    return out


# ── routes ──────────────────────────────────────────────────────────────────

@router.get("/api/projects")
async def list_projects():
    return await asyncio.to_thread(do_list_projects)


@router.post("/api/projects")
async def create_project(request: Request):
    body = await request.json()
    return await asyncio.to_thread(do_create, body)


@router.get("/api/jira/projects/{key}")
async def jira_project(key: str):
    key = key.strip().upper()
    if not projects.valid_jira_key(key):
        return _err(400, "not a valid Jira project key")
    cfg = projects.cfg_for(projects.get(min(p.id for p in projects.all_projects())))
    cfg["JIRA_PROJECT"] = key

    def look():
        proj = jira_api.get_project(cfg, key)
        if proj is None:
            return {"exists": False, "name": None, "boards": [], "issue_types": []}
        return {"exists": True, "name": proj.get("name"),
                "boards": jira_api.get_boards(cfg, key),
                "issue_types": [t["name"] for t in proj.get("issueTypes", [])]}
    try:
        return await asyncio.to_thread(look)
    except Exception as e:
        return _err(502, f"Jira lookup failed: {e}")


@router.patch("/api/p/{pid}")
async def patch_project(pid: int, request: Request):
    _project(pid)
    body = await request.json()
    try:
        p = await asyncio.to_thread(do_patch, pid, body)
    except ValueError as e:
        return _conflict_or_bad(e)
    return project_dict(p)


@router.delete("/api/p/{pid}")
async def delete_project(pid: int):
    _project(pid)
    try:
        return await asyncio.to_thread(do_delete, pid)
    except ValueError as e:
        return _err(400, str(e))


@router.post("/api/p/{pid}/fields/seed-defaults")
async def seed_defaults(pid: int):
    _project(pid)
    try:
        return await asyncio.to_thread(do_seed_defaults, pid)
    except ValueError as e:
        return _err(409, str(e))
    except RuntimeError as e:
        return _err(400, str(e))


@router.post("/api/p/{pid}/copy-from")
async def copy_from(pid: int, request: Request):
    _project(pid)
    body = await request.json()
    try:
        src = int(body.get("source"))
    except (TypeError, ValueError, AttributeError):
        return _err(400, "source (a project id) is required")
    return await asyncio.to_thread(do_copy, pid, src)


@router.post("/api/lark/resolve-link")
async def resolve_link(request: Request):
    body = await request.json()
    try:
        return await asyncio.to_thread(do_resolve_link, (body or {}).get("lark_base_link", ""))
    except ValueError as e:
        return _err(400, str(e))
    except Exception as e:
        log.warning(f"resolve-link failed: {e}")
        return _err(502, "Lark lookup failed (see server log)")


@router.get("/api/p/{pid}/people/suggestions")
async def people_suggestions(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_people_suggestions, pid)


@router.get("/api/p/{pid}/tables")
async def tables(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_tables, pid)


@router.get("/api/p/{pid}/lark-fields")
async def lark_fields(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_lark_fields, pid)


@router.get("/api/p/{pid}/jira-fields")
async def jira_fields(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_jira_fields, pid)


@router.get("/api/p/{pid}/jira-issue-types")
async def jira_issue_types(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_jira_issue_types, pid)


@router.get("/api/p/{pid}/lark-type-options")
async def lark_type_options(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_lark_type_options, pid)


@router.get("/api/p/{pid}/fields")
async def get_fields(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_fields, pid)


@router.post("/api/p/{pid}/fields")
async def save_field(pid: int, request: Request):
    _project(pid)
    return await asyncio.to_thread(do_save_field, pid, await request.json())


@router.delete("/api/p/{pid}/fields/{mapping_id}")
async def delete_field(pid: int, mapping_id: int):
    _project(pid)
    out = await asyncio.to_thread(do_delete_field, pid, mapping_id)
    if not out["ok"]:
        return _err(400, out["error"])
    return out


@router.get("/api/p/{pid}/sync-types")
async def get_sync_types(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_sync_types, pid)


@router.post("/api/p/{pid}/sync-types")
async def post_sync_types(pid: int, request: Request):
    _project(pid)
    return await asyncio.to_thread(do_save_sync_types, pid, await request.json())


@router.post("/api/p/{pid}/backfill")
async def backfill(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_backfill, pid)


@router.get("/api/p/{pid}/check")
async def check(pid: int):
    _project(pid)
    return await asyncio.to_thread(do_check, pid)


@router.get("/api/p/{pid}/people")
async def get_people(pid: int):
    return {"assignee_map": dict(_project(pid).assignee_map)}


@router.put("/api/p/{pid}/people")
async def put_people(pid: int, request: Request):
    _project(pid)
    body = await request.json()
    m = body.get("assignee_map") if isinstance(body, dict) else None
    if not _valid_str_map(m):
        return _err(400, "assignee_map must be a JSON object of string to string")
    p = await asyncio.to_thread(projects.update, pid, {"assignee_map": dict(m)})
    return {"assignee_map": dict(p.assignee_map)}
