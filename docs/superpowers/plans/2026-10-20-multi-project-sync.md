# Multi-project Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One deployment syncs any number of Jira-project ↔ Lark-Base-table pairs ("sync projects"), each configured and monitored from the dashboard, with project #1 behaving exactly as today.

**Architecture:** A new `projects.py` registry (Supabase table `sync_projects`) plus a `contextvars` "current project" set at every entry point (webhook handler, reconcile, backfill, API call). Module-level state that is per-pair today (index, field-mapping cache, sync types, assignee map, Jira lookup caches, reconcile watermarks) becomes a dict keyed by project id and is read through the current project, so the handler bodies keep their shape. A contextvar is per thread/task, so concurrent webhooks for different projects never see each other's state — this satisfies the spec's "no mutable global active project" rule.

**Tech Stack:** Python 3.11+, FastAPI, Supabase (supabase-py), requests, pytest.

**Spec:** `docs/superpowers/specs/2026-10-20-multi-project-sync-design.md`

**Base branch:** this branch sits on `refactor/workspace-profile-env` (draft PR techflowasia/jira-lark-webhook#6). #6 already moved the Lark app id/secret, Base token, bot open_id, public URL and Jira custom-field ids (`config.JF_*`) out of code. #6 merges first; this PR then targets `main`. Where #6 made something an env var that this spec makes per-project (`ASSIGNEE_MAP`, `DEFAULT_SYNC_TYPES`, `LARK_BASE_TOKEN`, `LARK_TABLE_ID`, `JIRA_PROJECT`), the env var becomes **only the seed for project #1** in `bootstrap_default()`.

**Test runner:** `/Users/TechFlow003/Desktop/Github/jira-lark-webhook/.venv/bin/python -m pytest` (system python has no pytest). Baseline: 141 passed. Below, `PYTEST` means that command.

## Global Constraints

- Project #1 must keep identical behaviour and data: no Jira or Lark record is written by migration or bootstrap.
- A field with no active mapping row in a project never syncs in any path (webhook create/update/delete, reconcile, backfill), built-ins included.
- Mappings for `summary`, `key`, `issuetype` (Lark Title, Jira Key, Type) cannot be deleted or deactivated.
- Jira assignee unassigned **or** not in the project's people list → Lark Assignee cleared; unmapped additionally logs a warning history row naming the person. Lark assignee with no Jira mapping → Jira unchanged + warning.
- New projects are created with `sync_enabled=false`, `reconcile_enabled=false`, `setup_state='draft'`.
- One Jira project key ↔ one project; one (`lark_base_token`, `lark_table_id`) ↔ one project (unique constraints).
- Unroutable webhooks are logged and ignored, never defaulted to another project (exception: the legacy `/webhook/lark-auto` URL is project #1's by definition).
- Reconcile every 6 h (21600 s), projects run sequentially; one project's failure doesn't stop the others.
- Dedup keys are NOT prefixed by project: Jira issue keys (`VR-12` vs `NEW-12`) and Lark record ids are globally unique already. (Deliberate deviation from the spec's table; the isolation test in Task 2 pins it.)
- Data Integrity Rule (`CLAUDE.md`): Task 9's verification on deployed data is part of done.

## Review Focus

1. Two webhooks for different projects processed concurrently in worker threads → each uses only its own mappings, index and people (Task 2 thread test).
2. Jira webhook for a project key with no project, or a project whose `sync_enabled` is false/draft → nothing written, history shows nothing for other projects (Task 5).
3. Supabase `field_mappings` missing a system row that project #1 relies on via today's `config.py` fallback → bootstrap inserts it so behaviour is unchanged once the fallback is removed (Task 1 + Task 3).
4. A mapped Lark column renamed/deleted in Lark after go-live → that field's write is dropped with a warning; the rest of the record still syncs (Task 3).
5. Two tables of the same Base configured as two projects → events route by (base, table), not by base alone (Task 1 `find_by_lark`, Task 5).

---

### Task 1: Project registry, migration, bootstrap

**Files:**
- Create: `projects.py`, `migrations/003_multi_project.sql`, `tests/test_projects.py`
- Modify: `config.py` (`get_cfg`), `main.py` (startup calls `projects.load_all()` + `projects.bootstrap_default()` before other loaders)

**Interfaces:**
- Produces (`projects.py`):
  - `@dataclass class Project: id: int; name: str; jira_project_key: str; jira_board_id: str | None; lark_base_token: str; lark_table_id: str; lark_table_name: str; sync_enabled: bool; reconcile_enabled: bool; allowed_jira_types: list[str]; allowed_lark_types: list[str]; assignee_map: dict[str, str]; setup_state: str`  (`setup_state` ∈ `"draft"`, `"active"`; `assignee_map` is `{jira_display_name: lark_name}`)
  - `load_all() -> None`, `all_projects() -> list[Project]` (ordered by id), `get(pid: int) -> Project | None`
  - `find_by_jira_key(project_key: str) -> Project | None`, `find_by_lark(base_token: str, table_id: str) -> Project | None`
  - `create(fields: dict) -> Project` (forces draft/off defaults; raises `ValueError` on duplicate jira key or base+table), `update(pid: int, fields: dict) -> Project`
  - `bootstrap_default() -> Project`
  - `use(pid: int)` — context manager setting the contextvar; `current() -> Project`; `current_id() -> int`
  - `cfg_for(p: Project) -> dict` — today's `get_cfg()` keys with project values, plus `"PROJECT_ID": p.id` and `"JIRA_BOARD_ID": p.jira_board_id`
- `config.get_cfg()` keeps its signature and returns `projects.cfg_for(projects.current())`.

- [ ] **Step 1: Write `migrations/003_multi_project.sql`** — `create table if not exists sync_projects` with the Project columns (`allowed_*` and `assignee_map` as `jsonb`, `created_at timestamptz default now()`), `unique(jira_project_key)`, `unique(lark_base_token, lark_table_id)`; `alter table field_mappings add column if not exists project_id bigint references sync_projects(id)`; `alter table sync_history add column if not exists project_id bigint`; index on `sync_history(project_id, ts desc)`. Schema only — data is seeded by `bootstrap_default()` because the seed values live in env vars.

- [ ] **Step 2: Write failing tests in `tests/test_projects.py`**

```python
def test_fallback_project_without_supabase(monkeypatch):
    # history._get_client() -> None
    projects.load_all()
    p = projects.current()
    assert p.id == 1 and p.jira_project_key == os.environ["JIRA_PROJECT"]
    assert p.assignee_map == config.JIRA_TO_LARK_ASSIGNEE
    assert set(p.allowed_jira_types) == set(config.DEFAULT_SYNC_TYPES)

def test_use_switches_and_restores():
    with projects.use(2): assert projects.current_id() == 2
    assert projects.current_id() == 1

def test_use_is_isolated_per_thread():   # Review Focus 1
    # thread A inside use(2) sleeps; main thread reads current_id() == 1 meanwhile

def test_find_by_lark_needs_table_match():   # Review Focus 5
    # projects 1 and 2 share base "B", tables "t1"/"t2"
    assert projects.find_by_lark("B", "t2").id == 2
    assert projects.find_by_lark("B", "t9") is None

def test_create_defaults_to_draft_and_off(): ...
def test_create_rejects_duplicate_jira_key(): ...   # ValueError

def test_bootstrap_idempotent(fake_supabase):
    p1 = projects.bootstrap_default(); p2 = projects.bootstrap_default()
    assert p1.id == p2.id and fake_supabase.count("sync_projects") == 1

def test_bootstrap_seeds_from_live_settings(fake_supabase):
    # settings: active_table_id="tblX", sync_enabled="false", allowed_jira_types='["Epic"]'
    p = projects.bootstrap_default()
    assert (p.lark_table_id, p.sync_enabled, p.allowed_jira_types) == ("tblX", False, ["Epic"])

def test_bootstrap_backfills_project_id_and_missing_system_rows(fake_supabase):  # Review Focus 3
    # field_mappings has 12 system rows (no "status"), project_id NULL
    projects.bootstrap_default()
    rows = fake_supabase.rows("field_mappings")
    assert all(r["project_id"] == 1 for r in rows)
    assert {r["jira_field"] for r in rows if r["is_system"]} == {m["jira_field"] for m in field_mappings._DEFAULTS}

def test_bootstrap_copies_reconcile_watermarks(fake_supabase):
    # settings last_reconcile_ts="123" -> "last_reconcile_ts:1" == "123"
```

`fake_supabase`: a small in-memory fixture in `tests/conftest.py` implementing the chained calls the code uses (`table().select().in_().eq().is_().order().range().execute()`, `insert`, `update`, `upsert`, `delete`), patched into `history._get_client`. Later tasks reuse it.

- [ ] **Step 3: Run** `PYTEST tests/test_projects.py -v` → FAIL (no module `projects`).

- [ ] **Step 4: Implement `projects.py`.** Without Supabase: a single in-memory Project id=1 built from env (`JIRA_PROJECT`, `LARK_BASE_TOKEN`, `LARK_TABLE_ID`) + `config.DEFAULT_SYNC_TYPES` + `config.JIRA_TO_LARK_ASSIGNEE`, `sync_enabled=True`, `reconcile_enabled=True`, `setup_state="active"`. `current()` outside any `use()` returns the lowest-id project. `bootstrap_default()` only acts when `sync_projects` is empty: insert project #1 from settings rows (`active_table_id/name`, `sync_enabled`, `reconcile_enabled`, `allowed_*_types`) falling back to env/config; `update field_mappings set project_id=1 where project_id is null`; insert any `_DEFAULTS` system row missing for project 1; copy `last_reconcile_ts`/`last_full_reconcile_ts` to `…:1`; `history.record(direction="system", event="config", description="Multi-project bootstrap: created project #1 …")`.

- [ ] **Step 5: Point `config.get_cfg()` at `projects`**; startup order in `main.py`: `load_all` → `bootstrap_default` → existing loaders.

- [ ] **Step 6: Run** `PYTEST` → all pass (141 old + new).

- [ ] **Step 7: Commit** `feat: sync project registry with project #1 bootstrap`.

---

### Task 2: Per-project state

**Files:**
- Modify: `index.py`, `config.py`, `field_mappings.py`, `lark_handler.py` (caches, `process` entry, update-coalescing keys), `jira_handler.py` (`process` entry), `reconcile.py` (`run`, `backfill`, watermarks), `lark_api.py` (`_base_tz_cache`), `history.py` (`record`, `query`)
- Test: `tests/test_project_isolation.py`

**Interfaces:**
- Consumes: `projects.use`, `projects.current`, `projects.current_id`, `projects.update`.
- Produces:
  - `index`: module `__getattr__` makes `index._jira_to_lark` / `index._lark_to_jira` return the current project's dicts (existing call sites and tests unchanged); `rebuild/add/remove_*` act on the current project; `count(pid: int) -> int`.
  - `config.get_allowed_jira_types() / get_allowed_lark_types()` read the current project; `config.save_sync_types(jira_types, lark_types)` writes the current project via `projects.update`.
  - `config.jira_to_lark_assignee() -> dict`, `config.lark_to_jira_assignee() -> dict` (current project). Remove the `from config import JIRA_TO_LARK_ASSIGNEE / LARK_TO_JIRA_ASSIGNEE` imports in handlers/reconcile; the module constants stay only as bootstrap seed.
  - `field_mappings`: `_cache: dict[int, list]`; `load(project_id: int | None = None)` (None = all); `get_all()`, `get_direction()`, `get_custom_*()` and `__getattr__` read the current project; `upsert(row)` / `delete(id)` stamp/scope `project_id = current_id()`; `copy_from(src_pid: int, dst_pid: int) -> int` (rows copied).
  - `lark_handler._account_ids_cache`, `_version_cache`, `_sprint_cache` keyed by project id; `_update_in_flight` / `_update_pending` keyed by `(pid, rid)`.
  - `jira_api.get_board_id(cfg)` returns `cfg["JIRA_BOARD_ID"]` when set, else today's first-board lookup.
  - `lark_api.get_base_tz_hours(token, base_token)` caches per `base_token`.
  - `reconcile` watermark keys `last_reconcile_ts:{pid}`, `last_full_reconcile_ts:{pid}`.
  - `history.record(...)` adds `project_id=current_id()`; `history.query(..., project_id: int | None = None)` filters when given; `history.error_count(project_id: int, since: datetime) -> int`.
  - Entry points `jira_handler.process`, `lark_handler.process`, `reconcile.run`, `reconcile.backfill` wrap their body in `with projects.use(cfg["PROJECT_ID"] if "PROJECT_ID" in cfg else projects.current_id()):` (existing test CFGs without `PROJECT_ID` keep working).

- [ ] **Step 1: Write failing tests in `tests/test_project_isolation.py`**

```python
def test_index_is_per_project():
    with projects.use(1): index.add("VR-1", "recA")
    with projects.use(2): assert "VR-1" not in index._jira_to_lark

def test_field_mappings_per_project():   # project 2 maps Title -> "Name"
    with projects.use(2): assert field_mappings.F_TITLE == "Name"
    with projects.use(1): assert field_mappings.F_TITLE == "Title"

def test_assignee_map_per_project(): ...
def test_sync_types_per_project(): ...

def test_jira_create_uses_its_projects_mappings(mock_lark):
    jira_handler.process("jira:issue_created", issue_NEW_1, {}, {**CFG, "PROJECT_ID": 2})
    fields = mock_lark.create_record.call_args[0][3]
    assert "Name" in fields and "Title" not in fields
    assert mock_lark.create_record.call_args[0][1:3] == ("base2", "tbl2")

def test_concurrent_handlers_do_not_cross(mock_lark):   # Review Focus 1
    # ThreadPoolExecutor: 20 interleaved creates for projects 1 and 2
    # every create_record call for a NEW-* key used ("base2","tbl2") and project-2 field names

def test_history_rows_carry_project_id(fake_supabase): ...
def test_reconcile_watermarks_per_project(fake_supabase): ...
def test_base_tz_cache_per_base(mock_requests): ...
```

- [ ] **Step 2: Run** `PYTEST tests/test_project_isolation.py -v` → FAIL.
- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run** `PYTEST` → all pass, including the 141 existing tests unmodified (except fixtures that reset now-per-project caches, if any must be adjusted — keep such edits to fixture setup only).
- [ ] **Step 5: Commit** `feat: per-project sync state behind a current-project context`.

---

### Task 3: The mapping table decides what syncs

**Files:**
- Modify: `field_mappings.py`, `utils.py`, `jira_handler.py`, `lark_handler.py`, `reconcile.py`, `lark_api.py` (`create_record`, `update_record`)
- Test: `tests/test_unmapped_fields.py`

**Interfaces:**
- Produces:
  - `field_mappings.__getattr__("F_*")` returns `None` when the current project has no **active** row for that system field (the `config.py` fallback is removed; `_DEFAULTS` is still used as project #1's rows when Supabase is unreachable).
  - `field_mappings.syncs(lark_field: str | None, direction: str) -> bool` — False for `None`; `direction` ∈ `"jira_to_lark"`, `"lark_to_jira"`; True when the row's direction is that or `"both"`.
  - `field_mappings.REQUIRED_JIRA_FIELDS = {"summary", "key", "issuetype"}`; `delete()` and `upsert(... active=False)` on those raise `ValueError("… is required")`.
  - `utils.put(fields: dict, name: str | None, value) -> None` — no-op when `name is None`.
  - `lark_api.create_record` / `update_record` drop keys not present in `list_fields(token, base, table)` names, logging `warning` + `history.record(status="skipped", description="Lark column '<name>' not found — field not synced")`. (Uses the existing 60 s schema cache.)
- Every system-field read/write in `jira_handler`, `lark_handler`, `reconcile` (find them with `grep -n "field_mappings.F_" jira_handler.py lark_handler.py reconcile.py`) goes through `put` for Lark writes and is gated by `syncs(F_X, "lark_to_jira")` for Jira writes. Gating must happen before the value is read, so an unmapped Lark field can never be read as "empty" and clear a Jira field.

- [ ] **Step 1: Write failing tests** (project 2 fixture maps only Title, Jira Key, Type, Assignee — the user's "only 4 fields" example)

```python
def test_jira_create_writes_only_mapped(mock_lark):
    # issue has story points, start/due dates, actual dates, status, sprint
    fields = <create_record fields>
    assert set(fields) == {"Title", "Jira Key", "Type", "Assignee"}

def test_jira_update_ignores_unmapped_changelog(mock_lark):
    # changelog: duedate + customfield_10016 changed
    mock_lark.update_record.assert_not_called()

def test_lark_update_never_clears_unmapped_jira_field(mock_jira):
    # Lark record has no "Timeline - End" column; full_snapshot=True
    assert "duedate" not in <update_issue fields>

def test_reconcile_skips_unmapped(mock_lark, mock_jira): ...
def test_backfill_create_skips_unmapped(mock_lark, mock_jira): ...

def test_required_mapping_cannot_be_deleted(fake_supabase):
    with pytest.raises(ValueError): field_mappings.delete(<id of key mapping>)

def test_project1_unchanged_with_full_rows(mock_lark):
    # project 1 with all 13 default rows: create_record fields == today's fields (copy assertion from test_create_makes_lark_record)

def test_write_drops_unknown_lark_column(mock_requests):   # Review Focus 4
    # schema lacks "R. MD"; update_record(..., {"Title": "x", "R. MD": 3}) sends {"Title": "x"} and logs skipped
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PYTEST` → all pass.
- [ ] **Step 5: Commit** `feat: only mapped fields sync; required mappings protected`.

---

### Task 4: Assignee rule

**Files:**
- Modify: `jira_handler.py` (create ~L59, update ~L137), `reconcile.py` (~L222/239, ~L419/436, ~L505/517), `lark_handler.py` (~L345, ~L490)
- Test: `tests/test_assignee_rule.py`

**Interfaces:**
- Produces: `config.lark_assignee_for(display_name: str | None) -> tuple[str | None, bool]`: returns `(lark_name_or_None, is_unmapped)`; `is_unmapped` is True only when `display_name` is set and absent from the current project's map.
- All Jira→Lark paths (create, update, reconcile, backfill) set Lark Assignee to `[lark_name]` or **clear** it (`None`) when the result is None; when `is_unmapped`, write one `history.record(status="skipped", event="updated", jira_key=…, description=f"Assignee '{name}' not in people list — Lark Assignee cleared")`.
- Lark→Jira unmapped: Jira unchanged + same style warning (today's behaviour plus the warning).

- [ ] **Step 1: Failing tests**: unmapped on update clears Lark + warning row; unassigned clears Lark, no warning; reconcile with unmapped clears (today it leaves unchanged — this is the intended change); mapped person still set; Lark→Jira unmapped leaves Jira unchanged + warning.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `PYTEST` → pass.
- [ ] **Step 5: Commit** `feat: consistent assignee rule — unmapped clears Lark with a warning`.

---

### Task 5: Webhook routing, per-project toggles, reconcile loop

**Files:**
- Create: `routing.py`, `tests/test_routing.py`
- Modify: `main.py` (`/webhook/jira`, `/webhook/lark`, `/webhook/lark-auto`, new `/webhook/lark-auto/{project_id}`, `/toggle*`, `_reconcile_loop`, `_initial_index_rebuild`, `_load_active_table_async` removed in favour of per-project table on Project, `_load_toggle_states` keeps only `lark_value_cache_enabled`)

**Interfaces:**
- Produces (`routing.py`):
  - `route_jira(body: dict) -> Project | None` — project key from `issue.fields.project.key`, fallback `issue.key.rsplit("-", 1)[0]`.
  - `route_lark_event(event: dict) -> Project | None` — `find_by_lark(event["file_token"], event["table_id"])`.
  - `route_lark_auto(body: dict, project_id: int | None) -> Project | None` — with id: that project (and `body.table_id`, if present, must equal its table); legacy (None): `find_by_lark(project1.base, body.table_id)` if `table_id` present, else project #1.
  - `is_live(p: Project | None) -> bool` — `p is not None and p.sync_enabled and p.setup_state == "active"`.
- Webhook handlers: route → if not live, log + append to `_raw_payloads` with `"routed": None` and return `{"ok": True, "note": "no live project"}` → else `bg.add_task(handler, ..., projects.cfg_for(p))`.
- `POST /p/{pid}/toggle`, `POST /p/{pid}/toggle/reconcile` flip and persist via `projects.update`, history row in that project, redirect to `/p/{pid}`. Old `/toggle`, `/toggle/reconcile` act on project #1.
- Startup builds each project's index (sequentially, errors per project logged). `_reconcile_loop` every 21600 s: `for p in projects.all_projects(): if p.reconcile_enabled and p.setup_state == "active": try reconcile.run(cfg_for(p)) except: log`.
- `/health` returns `{"ok": True, "projects": [{"id", "sync_enabled"}]}`.

- [ ] **Step 1: Failing tests** (`routing` pure functions + FastAPI `TestClient` for endpoints; add `httpx` to `requirements.txt` if `TestClient` needs it):
  - Jira body for `NEW-3` → project 2; for `ZZZ-1` → None and handler not scheduled (Review Focus 2).
  - Project 2 draft or sync off → not scheduled.
  - Lark event (`file_token` "B", `table_id` "t2") → project 2's cfg passed to `lark_handler.process`.
  - Legacy `/webhook/lark-auto` without `table_id` → project 1; `/webhook/lark-auto/2` with project 1's table → ignored.
  - Toggling project 2 leaves project 1's `sync_enabled` unchanged.
  - Reconcile loop body: project 1 raising doesn't stop project 2's run.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `PYTEST` → pass.
- [ ] **Step 5: Commit** `feat: route webhooks to their project; per-project toggles and reconcile`.

---

### Task 6: Project-scoped APIs and setup checks

**Files:**
- Create: `projects_api.py` (FastAPI `APIRouter`, included in `main.py`), `tests/test_projects_api.py`
- Modify: `main.py` — existing unscoped `/api/*` and `/settings/*` endpoints become thin aliases for project #1 that call the same functions.

**Interfaces** (every `/api/p/{pid}/…` handler runs inside `projects.use(pid)`; unknown pid → 404):
- `GET /api/projects` → `[{id, name, jira_project_key, setup_state, sync_enabled, reconcile_enabled, health}]`; `health` ∈ `"ok"`, `"error"` (`history.error_count(pid, now-24h) > 0`), `"paused"`, `"draft"` (precedence: draft > paused > error > ok).
- `POST /api/projects` body `{name?, jira_project_key, lark_base_link | lark_base_token, lark_table_id?, copy_from?}` → creates draft (Task 1 defaults); with `copy_from`, copies mappings (`field_mappings.copy_from`), sync types and assignee map.
- `PATCH /api/p/{pid}` → name, board, table, assignee_map, setup_state, sync types.
- `GET /api/p/{pid}/tables`, `/lark-fields`, `/jira-issue-types`, `/lark-type-options`, `/fields`; `POST /api/p/{pid}/fields`; `DELETE /api/p/{pid}/fields/{id}` (400 with message on required field); `GET|POST /api/p/{pid}/sync-types`; `POST /api/p/{pid}/backfill`.
- `GET /api/jira/projects/{key}` → `{exists, name, boards: [{id, name}], issue_types}` (new `jira_api.get_boards(cfg, project_key) -> list[dict]`, `jira_api.get_project(cfg, key) -> dict | None`).
- `GET /api/p/{pid}/check` → `{"fields": [{"mapping_id", "lark_field", "jira_field", "problem"}], "people": {"unmapped_jira": [...], "unknown_lark": [...]}, "ready": bool}`. Field problems: Lark column missing from table; Jira custom field id not in `jira_api.get_all_fields`; required mapping missing. People: assignable Jira users (`jira_api.get_account_ids(cfg)` keys) not in the map; map values not in the Lark Assignee options. `ready` = no field problems.
- `GET|PUT /api/p/{pid}/people` → assignee map; PUT validates JSON object of str→str.
- `lark_api.parse_base_link(token: str, url: str) -> tuple[str, str | None]`: `/base/{token}?table=tbl…` → `(token, table)`; `/wiki/{node}` → resolve via `GET /wiki/v2/spaces/get_node?token={node}` and require `obj_type == "bitable"` (`ValueError` otherwise); anything else → `ValueError("Not a Lark Base link")`.

- [ ] **Step 1: Failing tests:** create → draft/off; duplicate jira key → 409; copy_from copies 13 mappings into project 2 and leaves project 1's rows untouched; `/check` flags a mapping whose Lark column is absent and `ready` False; deleting required mapping → 400; `parse_base_link` for base URL, wiki URL (mocked), garbage; project-1 aliases return the same as `/api/p/1/...`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `PYTEST` → pass.
- [ ] **Step 5: Commit** `feat: project-scoped settings API and setup checks`.

---

### Task 7: Dashboard layout C

**Files:**
- Create: `dashboard.py` (move the HTML/CSS/JS rendering out of `main.py`'s `dashboard()`), `tests/test_dashboard.py`
- Modify: `main.py` routes: `GET /` → redirect to `/p/{last or first}` (last viewed via cookie `lp`), `GET /p/{pid}`, `GET /activity`

**Interfaces:**
- `dashboard.render_project(p: Project, request, history_result: dict) -> str`, `dashboard.render_activity(request, history_result: dict) -> str`, `dashboard.sidebar(projects_with_health: list[dict], selected: int | None) -> str`.
- Sidebar: rows `● name · KEY` with dot colours ok `#22c55e`, error `#ef4444`, paused `#f59e0b`, draft `#94a3b8`; then "+ New project" (`/p/new`), then "All activity" (`/activity`); footer: Lark value cache toggle, debug links. Collapses to a top drawer under 768 px.
- Project page: today's cards/sections, all JS calls rewritten to `/api/p/{pid}/…` and toggles to `/p/{pid}/toggle*`; new "People" section (table of Jira name → Lark name, add/remove rows, Save → `PUT /api/p/{pid}/people`, unmapped-count badge from `/api/p/{pid}/check`); draft projects show a banner linking to `/p/{pid}/setup`.
- All activity: today's history table + "Project" column + project `<select>` filter (query param `project`), same range/search/pagination.

- [ ] **Step 1: Failing tests** (TestClient, fake_supabase): `/p/2` HTML contains both project names in the sidebar and `/api/p/2/` URLs, and no `/api/p/1/`; `/activity?project=2` shows only project-2 rows; `/` redirects to `/p/1` with no cookie.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `PYTEST` → pass; then `uvicorn main:app --port 10000` and check `/p/1` in the browser pane at desktop and 375 px widths.
- [ ] **Step 5: Commit** `feat: sidebar dashboard with per-project pages and all-activity audit`.

---

### Task 8: New-project wizard and per-project Lark connect

**Files:**
- Create: `wizard.py` (HTML for steps), `tests/test_wizard.py`
- Modify: `main.py` (`/p/new`, `/p/{pid}/setup`, `/auth/start`, `/auth/callback`)

**Interfaces:**
- `GET /p/new` and `GET /p/{pid}/setup?step=N` render the spec's 9 steps (Name+Jira, Lark link+table, Connect Lark, Copy from, Check fields, People, Sync types, Backfill, Go live) using Task 6 APIs. The project row is created at the end of step 2 (draft/off). Step 5 blocks "Next" until `/api/p/{pid}/check` returns `ready`. Step 9 "Go live" `PATCH`es `setup_state="active", sync_enabled=true` (reconcile per checkbox) and shows the Jira-admin webhook reminder text: "If your Jira webhook is filtered by JQL to one project, add `<KEY>` to its filter in Jira → System → Webhooks."
- `/auth/start?project={pid}` puts `state=f"project:{pid}"`; `/auth/callback` parses it (default project 1 for the legacy `state=add_bot`) and uses that project's `lark_base_token` for the permission and subscribe calls; result page links back to `/p/{pid}/setup?step=4`.
- Step 3 also shows the per-project Automation URL `{PUBLIC_URL}/webhook/lark-auto/{pid}`.

- [ ] **Step 1: Failing tests:** `/auth/start?project=2` redirect contains `state=project%3A2`; callback with `state=project:2` posts to `/drive/v1/permissions/base2/members` and `/drive/v1/files/base2/subscribe` (mock urllib); legacy `state=add_bot` uses project 1's base; Go-live PATCH flips project 2 only.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `PYTEST` → pass; walk the wizard locally against a throwaway draft project (stop before Connect Lark).
- [ ] **Step 5: Update docs:** `CLAUDE.md` (file map: `projects.py`, `routing.py`, `projects_api.py`, `dashboard.py`, `wizard.py`; endpoints; Supabase `sync_projects`; assignee map now per project), `.env.example` comment that `JIRA_PROJECT`/`LARK_BASE_TOKEN`/`LARK_TABLE_ID`/`ASSIGNEE_MAP`/`DEFAULT_SYNC_TYPES` only seed project #1, `CHANGELOG.md`.
- [ ] **Step 6: Commit** `feat: new-project setup wizard with per-project Lark connect`.

---

### Task 9: Deploy and verify on real data (Data Integrity Rule)

Not code; required for done. Needs the user for the Supabase SQL and Jira admin step.

- [ ] **Step 1: Snapshot before deploy:** save `/debug/index` (linked count), `/api/fields`, `/api/sync-types`, `/debug/lark-calls` from production to the scratchpad.
- [ ] **Step 2:** User runs `migrations/003_multi_project.sql` in the Supabase SQL editor (schema only; safe before deploy).
- [ ] **Step 3:** Merge #6, then this PR; deploy via `/render-deploy`. Confirm the bootstrap history row, `/api/p/1/fields` == snapshot mappings (+ any inserted missing system rows, listed), sync types and toggles equal, index count equal.
- [ ] **Step 4:** Edit one test card each way in project #1 (Jira field change, Lark field change); confirm both sides match and history rows carry `project_id=1`; `/debug/lark-calls` rate in line with the snapshot over ~30 min.
- [ ] **Step 5:** User creates project #2 through the wizard (sync off) and runs Backfill; sample ≥5 records on both sides of project #2 for correct values; confirm project #1's index count and a sample of its records are unchanged.
- [ ] **Step 6:** User widens the Jira admin webhook if it's JQL-filtered; Go live on project #2; one edit each way in both projects; confirm each lands only in its own pair, and call counts stay bounded (no loop) for ~30 min.
- [ ] **Step 7:** Report results with the before/after numbers.
