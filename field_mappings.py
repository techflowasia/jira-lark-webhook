"""Field mapping cache — loads from Supabase, falls back to hardcoded defaults.

Mappings are per project (`field_mappings.project_id`). Every reader uses the
CURRENT project's rows (see projects.use); `field_mappings._cache` is that
project's list.
"""
import logging
import sys
import types
import config
import history
import projects

log = logging.getLogger(__name__)

# project id -> that project's mapping rows
_by_project: "dict[int, list]" = {}


def _rows() -> list:
    return _by_project.setdefault(projects.current_id(), [])

# jira_field is the STABLE identity of a system mapping — it never changes
# when a user renames the Lark-side field via the dashboard (only lark_field
# does). This maps each config.py constant name to that stable key, so
# module.__getattr__ below can resolve the CURRENT lark_field for it.
_SYSTEM_JIRA_KEYS = {
    "F_TITLE":        "summary",
    "F_START":        config.JF_START,
    "F_END":          "duedate",
    "F_ASSIGNEE":     "assignee",
    "F_JIRA_KEY":     "key",
    "F_JIRA_URL":     "url",
    "F_TYPE":         "issuetype",
    "F_PARENT":       "parent",
    "F_MD":           config.JF_STORY_POINTS,
    "F_RELEASE":      "fixVersions",
    "F_ACTUAL_START": config.JF_ACTUAL_START,
    "F_ACTUAL_END":   config.JF_ACTUAL_END,
    "F_JIRA_STATUS":  "status",
}


def __getattr__(name: str):
    """PEP 562 dynamic module attribute: `field_mappings.F_PARENT` (etc.)
    resolves to the CURRENT Lark field name for that system mapping, read
    fresh from the dashboard-editable cache on every access (module
    attribute lookups aren't cached the way `from x import y` bindings are,
    so this re-resolves every single call — no stale, import-time-frozen
    value). Falls back to the config.py default when the cache has no
    matching row (Supabase unreachable, row deleted, or cache not yet
    loaded) — same resilience contract as get_all()/load().

    This is what makes a dashboard field-name rename (e.g. "Parent items" ->
    "Epic") take effect immediately, with no code change or redeploy: every
    call site now does `field_mappings.F_PARENT` instead of importing a
    frozen string from config.py.
    """
    jira_field = _SYSTEM_JIRA_KEYS.get(name)
    if jira_field is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    for m in _rows():
        if m.get("jira_field") == jira_field and m.get("is_system"):
            return m.get("lark_field")
    return getattr(config, name)

# Hardcoded fallback (matches Supabase seed data)
_DEFAULTS = [
    {"id": 0, "lark_field": "Title",             "jira_field": "summary",           "jira_label": "Title",        "direction": "both",         "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Timeline - Start",  "jira_field": config.JF_START, "jira_label": "Start Date",   "direction": "both",         "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Timeline - End",    "jira_field": "duedate",           "jira_label": "Due Date",     "direction": "both",         "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Assignee",          "jira_field": "assignee",          "jira_label": "Assignee",     "direction": "both",         "field_type": "user",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Type",              "jira_field": "issuetype",         "jira_label": "Issue Type",   "direction": "both",         "field_type": "select", "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Parent items",      "jira_field": "parent",            "jira_label": "Parent",       "direction": "both",         "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Jira Key",          "jira_field": "key",               "jira_label": "Jira Key",     "direction": "jira_to_lark", "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Jira URL",          "jira_field": "url",               "jira_label": "Jira URL",     "direction": "jira_to_lark", "field_type": "text",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "R. MD",             "jira_field": config.JF_STORY_POINTS, "jira_label": "Story Points", "direction": "jira_to_lark", "field_type": "number", "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Release",           "jira_field": "fixVersions",       "jira_label": "Fix Version",  "direction": "lark_to_jira", "field_type": "select", "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Actual start date", "jira_field": config.JF_ACTUAL_START, "jira_label": "Actual Start", "direction": "jira_to_lark", "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Actual end date",   "jira_field": config.JF_ACTUAL_END, "jira_label": "Actual End",   "direction": "jira_to_lark", "field_type": "date",   "is_system": True,  "active": True},
    {"id": 0, "lark_field": "Jira status",       "jira_field": "status",            "jira_label": "Jira Status",  "direction": "jira_to_lark", "field_type": "select", "is_system": True,  "active": True},
]


def load(project_id: "int | None" = None) -> None:
    """Reload mappings for one project, or for every project when None.
    Rows without a project_id (pre-migration) belong to the lowest-id project,
    the same project bootstrap adopts them into. A project with no rows gets
    the hardcoded defaults."""
    pids = ([project_id] if project_id is not None
            else [p.id for p in projects.all_projects()])
    client = history._get_client()
    if not client:
        for pid in pids:
            _by_project[pid] = list(_DEFAULTS)
        return
    try:
        rows = client.table("field_mappings").select("*").order("id").execute().data or []
    except Exception as e:
        log.warning(f"field_mappings.load failed: {e}")
        for pid in pids:
            _by_project[pid] = list(_DEFAULTS)
        return
    owner = min(p.id for p in projects.all_projects())
    grouped: dict = {}
    for r in rows:
        pid = r.get("project_id")
        grouped.setdefault(owner if pid is None else int(pid), []).append(r)
    for pid in pids:
        _by_project[pid] = grouped.get(pid) or list(_DEFAULTS)


def get_all() -> list:
    return list(_rows())


def get_direction(lark_field: str) -> "str | None":
    """Direction configured for a specific Lark field (system or custom).

    Returns 'jira_to_lark', 'lark_to_jira', 'both', or None if the field has
    no mapping (e.g. a lark-only field). The system field mappings (e.g.
    'Jira status' with default direction 'jira_to_lark') are included — the
    direction is user-editable in the dashboard and must be honored by the
    system-field branches in lark_handler._handle_update_impl.

    Used by:
      - lark_handler._relevant_lark_fields() — gates whether to decode a
        system field's value from the webhook payload.
      - lark_handler._handle_update_impl() — gates whether to write a system
        field to Jira on update.
    """
    for m in _rows():
        if m.get("lark_field") == lark_field:
            return m.get("direction")
    return None


def get_custom_lark_to_jira() -> list:
    """Non-system active mappings that sync Lark → Jira."""
    return [m for m in _rows()
            if not m.get("is_system") and m.get("active")
            and m.get("direction") in ("both", "lark_to_jira")]


def get_custom_jira_to_lark() -> list:
    """Non-system active mappings that sync Jira → Lark."""
    return [m for m in _rows()
            if not m.get("is_system") and m.get("active")
            and m.get("direction") in ("both", "jira_to_lark")]


def upsert(row: dict) -> dict:
    """Insert or update a field mapping of the CURRENT project. Returns the
    saved row (empty when the id belongs to another project)."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    pid = projects.current_id()
    data = {k: row[k] for k in ("lark_field", "jira_field", "jira_label",
                                  "direction", "field_type", "is_system", "active")
            if k in row}
    if row.get("id"):
        res = (client.table("field_mappings").update(data)
               .eq("id", row["id"]).eq("project_id", pid).execute())
    else:
        res = client.table("field_mappings").insert({**data, "project_id": pid}).execute()
    load(pid)
    return (res.data or [{}])[0]


def delete(mapping_id: int) -> None:
    """Delete a mapping of the CURRENT project by id."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    pid = projects.current_id()
    client.table("field_mappings").delete().eq("id", mapping_id).eq("project_id", pid).execute()
    load(pid)


def update_lark_field(mapping_id: int, new_lark_field: str) -> None:
    """Update only the lark_field name of a system mapping (CURRENT project)."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    pid = projects.current_id()
    client.table("field_mappings").update({"lark_field": new_lark_field}) \
        .eq("id", mapping_id).eq("project_id", pid).execute()
    load(pid)


_COPY_COLS = ("lark_field", "jira_field", "jira_label", "direction",
              "field_type", "is_system", "active")


def copy_from(src_pid: int, dst_pid: int) -> int:
    """Copy every mapping row of project src_pid into project dst_pid.
    Returns the number of rows copied. Project src_pid is left untouched."""
    client = history._get_client()
    if not client:
        raise RuntimeError("Supabase not configured")
    src = (client.table("field_mappings").select("*")
           .eq("project_id", src_pid).order("id").execute().data or [])
    if src:
        client.table("field_mappings").insert(
            [{**{k: r.get(k) for k in _COPY_COLS}, "project_id": dst_pid} for r in src]).execute()
    load(dst_pid)
    return len(src)


class _Module(types.ModuleType):
    """`field_mappings._cache` reads and replaces the current project's rows."""

    # Assigning (or monkeypatch.setattr-ing) _cache replaces only the CURRENT
    # project's rows; it does not shadow the per-project store.
    @property
    def _cache(self) -> list:
        return _rows()

    @_cache.setter
    def _cache(self, rows: list) -> None:
        _by_project[projects.current_id()] = rows


sys.modules[__name__].__class__ = _Module
