"""Sync project registry: one row per Jira project <-> Lark table pair.

Backed by Supabase `sync_projects`; without Supabase a single in-memory
project #1 built from env/config is used, so behaviour matches the old
single-pair service. `use(pid)` scopes the current project to the calling
thread/task via a contextvar.
"""
import contextlib
import contextvars
import json
import logging
import os
import threading
from dataclasses import dataclass, field, fields as dc_fields

import config
import history

log = logging.getLogger(__name__)

_TABLE = "sync_projects"
_lock = threading.RLock()
_projects: dict = {}
_persisted: bool = False  # True when _projects mirrors rows in Supabase
_current_id: contextvars.ContextVar = contextvars.ContextVar("project_id", default=None)


@dataclass
class Project:
    id: int
    name: str
    jira_project_key: str
    jira_board_id: "str | None"
    lark_base_token: str
    lark_table_id: str
    lark_table_name: str
    sync_enabled: bool
    reconcile_enabled: bool
    allowed_jira_types: list = field(default_factory=list)
    allowed_lark_types: list = field(default_factory=list)
    assignee_map: dict = field(default_factory=dict)
    setup_state: str = "draft"


_COLUMNS = [f.name for f in dc_fields(Project)]
_UPDATABLE = set(_COLUMNS) - {"id"}


def _json(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return default if v is None else v


def _from_row(r: dict) -> Project:
    return Project(
        id=int(r["id"]),
        name=r.get("name") or "",
        jira_project_key=r.get("jira_project_key") or "",
        jira_board_id=(str(r["jira_board_id"]) if r.get("jira_board_id") not in (None, "") else None),
        lark_base_token=r.get("lark_base_token") or "",
        lark_table_id=r.get("lark_table_id") or "",
        lark_table_name=r.get("lark_table_name") or "",
        sync_enabled=bool(r.get("sync_enabled")),
        reconcile_enabled=bool(r.get("reconcile_enabled")),
        allowed_jira_types=list(_json(r.get("allowed_jira_types"), [])),
        allowed_lark_types=list(_json(r.get("allowed_lark_types"), [])),
        assignee_map=dict(_json(r.get("assignee_map"), {})),
        setup_state=r.get("setup_state") or "draft",
    )


def _to_row(p: Project) -> dict:
    row = {k: getattr(p, k) for k in _COLUMNS if k != "id"}
    return row


def _env_project() -> Project:
    """Project #1 as the old single-pair service saw it (env + config)."""
    return Project(
        id=1,
        name=os.environ.get("JIRA_PROJECT", "") or "Default",
        jira_project_key=os.environ.get("JIRA_PROJECT", ""),
        jira_board_id=None,
        lark_base_token=os.environ.get("LARK_BASE_TOKEN", ""),
        lark_table_id=os.environ.get("LARK_TABLE_ID", ""),
        lark_table_name="",
        sync_enabled=True,
        reconcile_enabled=True,
        allowed_jira_types=list(config.DEFAULT_SYNC_TYPES),
        allowed_lark_types=list(config.DEFAULT_SYNC_TYPES),
        assignee_map=dict(config.JIRA_TO_LARK_ASSIGNEE),
        setup_state="active",
    )


def load_all() -> None:
    """(Re)load the registry from Supabase; fall back to the env project."""
    global _projects, _persisted
    client = history._get_client()
    loaded: dict = {}
    if client:
        try:
            rows = client.table(_TABLE).select("*").order("id").execute()
            loaded = {int(r["id"]): _from_row(r) for r in (rows.data or [])}
        except Exception as e:
            log.warning(f"projects.load_all failed (is migration 003 applied?): {e}")
    persisted = bool(loaded)
    if not loaded:
        env = _env_project()
        loaded = {env.id: env}
    with _lock:
        _projects = loaded
        _persisted = persisted


def _ensure_loaded() -> None:
    if not _projects:
        load_all()


def all_projects() -> list:
    _ensure_loaded()
    with _lock:
        return [_projects[k] for k in sorted(_projects)]


def get(pid: int) -> "Project | None":
    _ensure_loaded()
    return _projects.get(pid)


def find_by_jira_key(project_key: str) -> "Project | None":
    for p in all_projects():
        if p.jira_project_key == project_key:
            return p
    return None


def find_by_lark(base_token: str, table_id: str) -> "Project | None":
    for p in all_projects():
        if p.lark_base_token == base_token and p.lark_table_id == table_id:
            return p
    return None


def _check_unique(fields: dict, exclude_id: "int | None" = None) -> None:
    for p in all_projects():
        if p.id == exclude_id:
            continue
        if (fields.get("jira_project_key")
                and fields["jira_project_key"].upper() == p.jira_project_key.upper()):
            raise ValueError(f"Jira project {p.jira_project_key} is already synced by project #{p.id}")
        if (fields.get("lark_base_token") and fields.get("lark_table_id")
                and fields["lark_base_token"] == p.lark_base_token
                and fields["lark_table_id"] == p.lark_table_id):
            raise ValueError(f"Lark table {p.lark_table_id} is already synced by project #{p.id}")


def _warn_memory_only(op: str, pid: "int | None" = None) -> None:
    if history._get_client():  # Supabase reachable but registry not persisted
        log.warning(f"projects.{op}{'' if pid is None else f' (project {pid})'}: registry is not "
                    "persisted (migration 003 applied but bootstrap not complete?) - "
                    "change is memory-only and will be lost on restart")


def create(fields: dict) -> Project:
    """Create a project. Always starts draft with sync and reconcile off."""
    global _persisted
    _ensure_loaded()
    data = {k: v for k, v in fields.items() if k in _UPDATABLE}
    data.update(sync_enabled=False, reconcile_enabled=False, setup_state="draft")
    if isinstance(data.get("jira_project_key"), str):
        data["jira_project_key"] = data["jira_project_key"].strip().upper()  # R9a
    for req in ("jira_project_key", "lark_base_token", "lark_table_id"):
        if not data.get(req):
            raise ValueError(f"{req} is required")
    _check_unique(data)
    data.setdefault("name", data["jira_project_key"])
    data.setdefault("jira_board_id", None)
    data.setdefault("lark_table_name", "")
    data.setdefault("allowed_jira_types", list(config.DEFAULT_SYNC_TYPES))
    data.setdefault("allowed_lark_types", list(config.DEFAULT_SYNC_TYPES))
    data.setdefault("assignee_map", {})
    with _lock:
        client = history._get_client() if _persisted else None
        if not client:
            _warn_memory_only("create")
        if client:
            res = client.table(_TABLE).insert(data).execute()
            p = _from_row(res.data[0])
        else:
            pid = max(_projects) + 1 if _projects else 1
            p = _from_row({"id": pid, **data})
        _projects[p.id] = p
    return p


def update(pid: int, fields: dict) -> Project:
    _ensure_loaded()
    with _lock:
        cur = _projects.get(pid)
        if cur is None:
            raise KeyError(f"project {pid} not found")
        changes = {k: v for k, v in fields.items() if k in _UPDATABLE}
        _check_unique({**_to_row(cur), **changes}, exclude_id=pid)
        merged = {**{k: getattr(cur, k) for k in _COLUMNS}, **changes}
        new = _from_row(merged)
        client = history._get_client() if _persisted else None
        if not client:
            _warn_memory_only("update", pid)
        if client and changes:
            client.table(_TABLE).update(changes).eq("id", pid).execute()
        _projects[pid] = new
        return new


def delete(pid: int) -> None:
    """Delete a DRAFT project: its registry row and its field_mappings rows,
    nothing else. Never touches Jira or Lark records. Project #1 is never
    deletable. Raises KeyError (unknown) / ValueError (#1 or not a draft)."""
    _ensure_loaded()
    with _lock:
        cur = _projects.get(pid)
        if cur is None:
            raise KeyError(f"project {pid} not found")
        if pid == 1:
            raise ValueError("project 1 cannot be deleted")
        if cur.setup_state != "draft":
            raise ValueError("only a draft project can be deleted")
        client = history._get_client()
        if client:
            client.table("field_mappings").delete().eq("project_id", pid).execute()
            if _persisted:
                client.table(_TABLE).delete().eq("id", pid).execute()
        del _projects[pid]
    import field_mappings
    field_mappings._by_project.pop(pid, None)


@contextlib.contextmanager
def use(pid: int):
    token = _current_id.set(pid)
    try:
        yield
    finally:
        _current_id.reset(token)


def current_id() -> int:
    pid = _current_id.get()
    if pid is not None:
        return pid
    _ensure_loaded()
    return min(_projects)


def current() -> Project:
    pid = current_id()
    p = get(pid)
    if p is None:
        raise RuntimeError(f"project {pid} is not in the registry")
    return p


def cfg_for(p: Project) -> dict:
    return {
        "JIRA_EMAIL":      os.environ["JIRA_EMAIL"],
        "JIRA_TOKEN":      os.environ["JIRA_TOKEN"],
        "JIRA_DOMAIN":     os.environ["JIRA_DOMAIN"],
        "JIRA_PROJECT":    p.jira_project_key,
        "LARK_APP_ID":     os.environ["LARK_APP_ID"],
        "LARK_APP_SECRET": os.environ["LARK_APP_SECRET"],
        "LARK_BASE_TOKEN": p.lark_base_token,
        "LARK_TABLE_ID":   p.lark_table_id,
        "PROJECT_ID":      p.id,
        "JIRA_BOARD_ID":   p.jira_board_id,
    }


def _bool_setting(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).lower() != "false"


def _list_setting(v, default: list) -> list:
    if v is None:
        return list(default)
    out = _json(v, None)
    return list(out) if isinstance(out, list) else list(default)


def bootstrap_default() -> Project:
    """Create project #1 from the live single-pair config when the registry
    is empty. Never writes to Jira or Lark. Idempotent."""
    client = history._get_client()
    if not client:
        _ensure_loaded()
        return current()
    existing = client.table(_TABLE).select("*").order("id").execute().data or []

    keys = ["active_table_id", "active_table_name", "sync_enabled", "reconcile_enabled",
            "allowed_jira_types", "allowed_lark_types",
            "last_reconcile_ts", "last_full_reconcile_ts"]
    rows = client.table("settings").select("key,value").in_("key", keys).execute().data or []
    kv = {r["key"]: r["value"] for r in rows}

    created = False
    if existing:
        pid = min(int(r["id"]) for r in existing)
    else:
        base = _env_project()
        row = {
            "name": base.name,
            "jira_project_key": base.jira_project_key,
            "jira_board_id": None,
            "lark_base_token": base.lark_base_token,
            "lark_table_id": kv.get("active_table_id") or base.lark_table_id,
            "lark_table_name": kv.get("active_table_name") or "",
            "sync_enabled": _bool_setting(kv.get("sync_enabled"), True),
            "reconcile_enabled": _bool_setting(kv.get("reconcile_enabled"), True),
            "allowed_jira_types": _list_setting(kv.get("allowed_jira_types"), config.DEFAULT_SYNC_TYPES),
            "allowed_lark_types": _list_setting(kv.get("allowed_lark_types"), config.DEFAULT_SYNC_TYPES),
            "assignee_map": base.assignee_map,
            "setup_state": "active",
        }
        res = client.table(_TABLE).insert(row).execute()
        pid = int(res.data[0]["id"])
        created = True

    # Idempotent repair steps: safe to rerun after a partial earlier run.
    # Adopt pre-multi-project history rows so per-project history views and
    # error counts include them.
    client.table("sync_history").update({"project_id": pid}).is_("project_id", "null").execute()
    # Adopt existing field mappings (every run).
    client.table("field_mappings").update({"project_id": pid}).is_("project_id", "null").execute()
    # Add any system mapping the table lacks — only in the run that creates
    # project #1 (R4). Afterwards the mapping table decides what syncs: a
    # built-in mapping the user deleted must stay deleted across restarts.
    if created:
        have = {r["jira_field"] for r in (client.table("field_mappings").select("jira_field,is_system")
                .eq("project_id", pid).execute().data or []) if r.get("is_system")}
        import field_mappings
        for d in field_mappings._DEFAULTS:
            if d["jira_field"] not in have:
                client.table("field_mappings").insert(
                    {**{k: v for k, v in d.items() if k != "id"}, "project_id": pid}).execute()

    # Per-project reconcile watermarks, carried over from the global ones
    # only when the per-project key is absent.
    scoped = [f"{k}:{pid}" for k in ("last_reconcile_ts", "last_full_reconcile_ts")]
    present = {r["key"] for r in (client.table("settings").select("key,value")
               .in_("key", scoped).execute().data or [])}
    for k in ("last_reconcile_ts", "last_full_reconcile_ts"):
        if kv.get(k) is not None and f"{k}:{pid}" not in present:
            client.table("settings").upsert({"key": f"{k}:{pid}", "value": kv[k]}).execute()

    load_all()
    if created:
        p = _projects[pid]
        history.record(direction="system", event="config",
                       description=f"Multi-project bootstrap: created project #{pid} "
                                   f"({p.jira_project_key} <-> {p.lark_table_id}) from existing config")
    return _projects[pid]
