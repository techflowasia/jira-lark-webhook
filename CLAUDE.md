# Project: jira-lark-webhook

## Scope Rule

**Stay inside this project directory at all times.**

- Never read, edit, or reference files outside `/Users/TechFlow003/Desktop/Github/jira-lark-webhook/`
- Do not navigate to sibling projects (`verve-api/`, `verve-admin/`, `gstack/`, etc.)
- All work — file reads, edits, shell commands, searches — must operate within this directory only

---

## Data Integrity Rule

**This project's whole purpose is keeping Jira and Lark data in sync. Data loss or divergence is a critical failure, not a cosmetic one.**

For every change — new requirement OR bug fix — you must verify data is not ruined before considering the task done:

- After any change that touches a sync path (handlers, reconcile, decoders, field mappings, API clients), check that real records are not lost, blanked, duplicated, or diverged between Jira and Lark.
- When a fix repairs a bug, also repair the data the bug already corrupted (e.g. bad field values, garbage schema options) — fixing the code is not enough if its damage remains.
- Verify on the actual deployed data (sample affected records on both sides), not just unit tests. Confirm both sides hold the same, correct values.
- Watch for sync loops and runaway writes after any change: monitor `/debug/lark-calls` and `/debug/payloads`; a stable, bounded call count is part of "done".
- If a change could blank or drop a field for existing records, migrate/repair those records as part of the same task — never leave records mid-corrupted.

A change that passes tests but loses or diverges real sync data is **not** complete.

---

## What This Project Does

A bidirectional webhook sync service that keeps Jira issues and Lark Base records in sync in real time. When a record is created/updated/deleted in either system, the change is mirrored to the other side within seconds. A 30-minute reconcile loop acts as a safety net for missed webhooks.

**Deployed at:** `https://jira-lark-webhook.onrender.com` (Render free tier — kept alive by self-ping + GitHub Actions cron)

---

## Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11+ |
| Framework | FastAPI + Uvicorn |
| Persistence | Supabase (PostgreSQL) |
| HTTP client | `requests` |
| Testing | pytest + pytest-asyncio |
| Deploy | Render (free web service) |

---

## File Map

| File | Role |
|------|------|
| `main.py` | FastAPI app, webhook endpoints, dashboard HTML, startup tasks |
| `config.py` | Env var loading, field name constants, assignee maps, sync-type config |
| `lark_handler.py` | Lark → Jira: handles `record_added`, `record_edited`, `record_deleted` |
| `jira_handler.py` | Jira → Lark: handles `jira:issue_created`, `jira:issue_updated`, `jira:issue_deleted` |
| `lark_api.py` | Lark OpenAPI client (token caching, CRUD on bitable records/fields/tables) |
| `jira_api.py` | Jira REST API v3 + Agile API client (issues, versions, sprints, fields) |
| `reconcile.py` | 30-min reconcile loop + `backfill()` for pre-existing records |
| `index.py` | In-memory bidirectional map: `jira_key ↔ lark_record_id` |
| `dedup.py` | TTL dedup cache (120 s) to break sync loops |
| `history.py` | Sync event log — writes to Supabase `sync_history`, falls back to in-memory deque |
| `field_mappings.py` | Per-project field mapping cache — loads from Supabase, falls back to hardcoded defaults (project #1 only). `copy_from`, `seed_defaults` |
| `projects.py` | Project registry (`sync_projects`), `use(pid)` current-project context, `create`/`update`/`delete` (draft only), `cfg_for` |
| `routing.py` | Routes Jira webhooks by project key and Lark events by base/table (or `/webhook/lark-auto/{pid}`) to a project; unknown → ignored |
| `projects_api.py` | Project-scoped REST API (`/api/projects`, `/api/p/{pid}/...`); the `do_*` functions are also used by the legacy unscoped aliases |
| `dashboard.py` | Sidebar layout, per-project pages, All activity audit |
| `wizard.py` | New-project setup wizard (9 steps) rendered in the sidebar layout |
| `utils.py` | Field parse helpers: `_lark_text`, `_lark_select`, date/timestamp converters |
| `migrations/` | One-off SQL migrations for the Supabase schema (run manually in the SQL editor) |
| `tests/` | pytest suite (`pytest tests/`): handlers, reconcile, routing, projects, projects API, dashboard, wizard, utils |

---

## Webhook Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/webhook/lark` | Lark Base event subscription (bot file-access required) |
| `POST` | `/webhook/lark-auto` | Lark Base Automation webhook (no bot access needed) |
| `POST` | `/webhook/jira` | Jira webhook receiver |
| `GET` | `/` | Dashboard (status, history, config, field mappings, sync types) |
| `GET` | `/health` | Health check (used by keepalive ping) |
| `POST` | `/toggle` | Enable/disable sync |
| `POST` | `/toggle/reconcile` | Enable/disable reconcile loop |
| `GET` | `/api/tables` | List Lark Base tables |
| `POST` | `/settings/table` | Switch active Lark table + rebuild index |
| `GET` | `/api/fields` | Get all field mappings |
| `POST` | `/settings/fields` | Upsert a field mapping |
| `DELETE` | `/settings/fields/{id}` | Delete a field mapping |
| `GET` | `/api/lark-fields` | List fields in active Lark table |
| `GET` | `/api/jira-fields` | List all Jira fields |
| `GET` | `/api/jira-issue-types` | List Jira issue types for the project |
| `GET` | `/api/lark-type-options` | Get Lark "Type" field options |
| `GET` | `/api/sync-types` | Get allowed sync types (Jira + Lark) |
| `POST` | `/settings/sync-types` | Save allowed sync types |
| `POST` | `/api/backfill` | Backfill pre-existing unlinked records |
| `GET` | `/debug/payloads` | Last 20 raw webhook payloads |
| `GET` | `/debug/index` | Current in-memory Jira↔Lark index |
| `POST` | `/debug/rebuild` | Force rebuild the in-memory index |
| `GET` | `/auth/start?project={pid}` | Lark OAuth flow start; `state=project:{pid}` (no param → legacy `add_bot`) |
| `GET` | `/auth/callback` | Lark OAuth callback: adds the bot as editor and subscribes to events on **that project's** Base (legacy `add_bot` → project #1); links back to `/p/{pid}/setup?step=4` |

### Multi-project endpoints

The unscoped endpoints above remain as aliases for project #1.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/p/new`, `/p/{pid}/setup?step=N` | Setup wizard (steps 1-9); unknown pid → 404 |
| `GET` | `/p/{pid}`, `/activity` | Project page; All activity (`?project=` filter) |
| `POST` | `/p/{pid}/toggle`, `/p/{pid}/toggle/reconcile` | Per-project sync / reconcile toggles |
| `POST` | `/webhook/lark-auto/{pid}` | Per-project Lark Automation webhook |
| `GET`/`POST` | `/api/projects` | List projects / create a draft (key upper-cased; sync off; 409 on duplicate key or table) |
| `GET` | `/api/jira/projects/{key}` | `{exists, name, boards, issue_types}` |
| `POST` | `/api/lark/resolve-link` | Read-only: Base token, table and table list from a pasted link |
| `PATCH` | `/api/p/{pid}` | name, board, table, assignee_map, sync types, `sync_enabled`, `reconcile_enabled`, `setup_state` (`active` → 400 while a required mapping is missing; going live rebuilds the index) |
| `DELETE` | `/api/p/{pid}` | Delete a **draft** (registry row + its field mappings only; project 1 refused; never touches Jira/Lark) |
| `GET` | `/api/p/{pid}/tables`, `/lark-fields`, `/jira-fields`, `/jira-issue-types`, `/lark-type-options` | Schema lookups |
| `GET`/`POST`/`DELETE` | `/api/p/{pid}/fields[/{id}]` | Field mappings |
| `POST` | `/api/p/{pid}/fields/seed-defaults` | Seed the 13 default rows (409 if the project already has rows) |
| `POST` | `/api/p/{pid}/copy-from` | `{source}`: copy mappings, sync types, people (409 if destination has rows) |
| `GET` | `/api/p/{pid}/check` | `{fields, people, ready}` setup check |
| `GET`/`PUT` | `/api/p/{pid}/people`, `GET /api/p/{pid}/people/suggestions` | Assignee map; suggestions |
| `GET`/`POST` | `/api/p/{pid}/sync-types` | Allowed types |
| `POST` | `/api/p/{pid}/backfill` | Backfill (the only wizard step that writes Jira/Lark records) |

---

## Environment Variables (see `.env.example`)

```
JIRA_EMAIL          # Jira account email (API auth)
JIRA_TOKEN          # Jira API token
JIRA_DOMAIN         # e.g. your-org.atlassian.net
JIRA_PROJECT        # e.g. PROJ
LARK_APP_ID         # Lark bot app ID
LARK_APP_SECRET     # Lark bot app secret
LARK_BASE_TOKEN     # Lark Base (bitable) token
LARK_TABLE_ID       # Default active table ID
SUPABASE_URL        # Supabase project URL
SUPABASE_KEY        # Supabase service role key
```

### Workspace profile (optional, `config.py`)

Nothing workspace-specific is hardcoded in the sync code. Each value below is an
env var whose default is the original workspace's value, so an unset var changes
nothing. `tests/test_config.py` pins both the defaults and the overrides.

| Env var | Default | Used for |
|---------|---------|----------|
| `JIRA_FIELD_START_DATE` | `customfield_10015` | Jira Start date |
| `JIRA_FIELD_STORY_POINTS` | `customfield_10016` | Story Points → `R. MD` |
| `JIRA_FIELD_ACTUAL_START` | `customfield_10175` | Actual start |
| `JIRA_FIELD_ACTUAL_END` | `customfield_10176` | Actual end |
| `JIRA_FIELD_SPRINT` | `customfield_10020` | Sprint → `Release` |
| `ASSIGNEE_MAP` | the five names below | JSON, Jira display name → Lark name |
| `DEFAULT_SYNC_TYPES` | `["Epic", "Story", "Task"]` | Allowed types before the dashboard sets any |
| `LARK_API_BASE` | `https://open.larksuite.com/open-apis` | Lark vs Feishu host |
| `LARK_BOT_OPEN_ID` | the original bot | Bot added as editor by `/auth/callback` |
| `PUBLIC_URL` | `https://jira-lark-webhook.onrender.com` | OAuth redirect and the webhook URLs shown on the dashboard |
| `TZ_OFFSET_HOURS` | `7` | History timestamps, daily call counter, reconcile JQL |

The OAuth flow in `main.py` reads `LARK_APP_ID`, `LARK_APP_SECRET` and
`LARK_BASE_TOKEN` from the same env vars as the sync paths.

The system rows in Supabase `field_mappings` are matched by `jira_field`. When
a `JIRA_FIELD_*` var is overridden, update that row's `jira_field` to the same
ID, or a dashboard rename of its Lark field stops being honored.

One deployment syncs many Jira project ↔ Lark table pairs. `JIRA_PROJECT`, `LARK_BASE_TOKEN`, `LARK_TABLE_ID`, `ASSIGNEE_MAP` and `DEFAULT_SYNC_TYPES` only seed project #1; every further project is created in the wizard and lives in `sync_projects`.

### New-project wizard (`/p/new`)

Draft (sync off) from the end of step 2; nothing syncs until Go live. 1 Name + Jira key/board → 2 Lark link + table (creates the draft) → 3 Connect Lark (`/auth/start?project={pid}`, plus the `/webhook/lark-auto/{pid}` URL) → 4 Copy from a project, or Start from defaults (Delete draft redoes a failed copy) → 5 Check fields (Next blocked until `/check` is `ready`) → 6 People → 7 Sync types → 8 Backfill → 9 Go live (`PATCH setup_state=active, sync_enabled=true`; index rebuilt). Remind the Jira admin to widen a JQL-filtered webhook to the new key.

---

## Supabase Tables

| Table | Purpose |
|-------|---------|
| `sync_history` | Event log (direction, event, jira_key, lark_id, type, description, status, error, ts). The `type` column (Epic / Story / Task / …) is added by `migrations/001_add_type_column.sql`. |
| `sync_projects` | Project registry: one row per Jira project ↔ Lark table (name, `jira_project_key`, board, `lark_base_token`, `lark_table_id`, sync/reconcile flags, allowed types, `assignee_map`, `setup_state` draft/active). Unique on key and on (base, table). Created by `migrations/003_multi_project.sql`; project #1 is bootstrapped from env + `settings` at startup. |
| `field_mappings` | Configurable Lark↔Jira field mappings, per project (`project_id`, migration 003) |
| `settings` | Key-value store. The per-project values now live in `sync_projects`; the old keys only seed project #1 once |

---

## Default Field Mappings (system, from `field_mappings.py`)

| Lark Field | Jira Field | Direction |
|------------|-----------|-----------|
| Title | summary | both |
| Timeline - Start | customfield_10015 (Start Date) | both |
| Timeline - End | duedate | both |
| Assignee | assignee | both |
| Type | issuetype | both |
| Parent items | parent | both |
| Jira Key | key | jira → lark |
| Jira URL | url | jira → lark |
| R. MD | customfield_10016 (Story Points) | jira → lark |
| Release | fixVersions | lark → jira |
| Actual start date | customfield_10175 | jira → lark |
| Actual end date | customfield_10176 | jira → lark |
| Jira status | status | jira → lark |

**The mapping table decides what syncs.** A field with no active mapping row in a project never syncs in any path (webhooks, reconcile, backfill) — built-in fields included. **Title (`summary`), Jira Key (`key`) and Type (`issuetype`) are required**: they cannot be deleted/deactivated, and `setup_state="active"` and backfill are refused while one is missing.

**Sprint field (customfield_10020) is read-only via Agile API** — skipped in issue update loop; moved via `jira_api.move_to_sprint()`.

---

## Assignee Mapping (per project; default of `ASSIGNEE_MAP` seeds project #1)

Rule (Jira → Lark, every path: webhook create/update, reconcile, backfill; `config.lark_assignee_for`):
- **Unassigned** → a **webhook update** clears the Lark Assignee; **sweeps** (reconcile/backfill) leave Lark unchanged (ruling R8).
- **In the people list** → the mapped Lark name is written.
- **Ignored** (in the people list with Lark name `""`, set with the People editor's *Ignore* checkbox, or `""` in `ASSIGNEE_MAP`) → Lark Assignee left unchanged on every path: never cleared, never written, never auto-added, no Lark option added, no history row. `config.lark_to_jira_assignee()` / `LARK_TO_JIRA_ASSIGNEE` skip empty/blank values, so an empty Lark Assignee never resolves to an ignored Jira user. The People editors refuse to save a row with no Lark name unless it is ticked Ignore.
- **Not in the people list** → **auto-added**: `{displayName: displayName}` joins the project's `assignee_map` (`projects.add_assignee`: under the registry lock, re-reads the current map, never overwrites an entry), `[displayName]` is written to Lark (via `utils.put`, so an unmapped Assignee field is skipped and nothing is added), and one `config` history row `Added '<name>' to People list and Lark Assignee` is written the first time. Nothing is ever cleared for that person. Exception: if `displayName` is already **another** person's Lark name, nothing is added or written (Lark → Jira would become ambiguous) and one skipped row is logged per process.
- If Lark rejects the record write because the name is not yet an option of the Assignee select column, `lark_api` adds the option (fresh schema read; the field PUT resends **every** existing option with its id, name and colour, plus the new one) and retries the write once. A write that succeeds never touches the column.

Lark → Jira: a Lark assignee with no Jira mapping leaves Jira unchanged + a rate-limited warning row.


| Jira display name | Lark name |
|-------------------|-----------|
| Tawan Vongsombun | Tawan |
| Thet Swe Lin | Lin |
| Benyapha Kasemtanakitti | Nurse |
| Moe Pyae Pyae Kyaw | Iris |
| Waritsara Matnok | Min |

---

## Sync Type Filtering

Only records/issues whose `Type`/`issuetype` is in the allowed set are synced.

- Default allowed: `Epic`, `Story`, `Task`
- Configurable at runtime via dashboard → "Sync Types" section
- Persisted in Supabase `settings` table keys `allowed_jira_types`, `allowed_lark_types`

---

## Key Design Patterns

### Dedup Loop Prevention (`dedup.py` + value comparison)
Two layers prevent sync loops:
1. **TTL dedup (`dedup.py`)** — before any write to Jira or Lark, the originating key is `dedup.mark()`'d with a 120 s TTL. When the mirrored webhook arrives within the window, `dedup.is_ours()` returns `True` and the handler skips it. Used for `create` and `delete` paths.
2. **Value comparison (update handlers)** — for `update` events, the handler fetches the current target-side record and only writes fields whose value actually differs. This is the authoritative loop guard for updates (replaces TTL-only dedup, which dropped legitimate edits made during the TTL window). See `jira_handler._handle_update` and `lark_handler._handle_update`.

### In-memory Index (`index.py`)
`_jira_to_lark` and `_lark_to_jira` dicts are rebuilt from all Lark records on startup and updated incrementally on every create/delete. Links discovered at update time (auto-discover via Jira Key field).

### Reconcile Loop (`reconcile.py`)
Runs every 30 minutes. Fetches all Lark records + Jira issues, removes duplicate Lark records (same Jira Key), deletes Lark records for gone Jira issues (blocked if >10 at once), creates/updates missing Lark records. `backfill()` is the one-shot version triggered from the dashboard.

### Timestamps
Lark stores dates as millisecond UTC timestamps. Bangkok timezone (UTC+7) is used for all date conversions (`_BKK` in `utils.py`, `_TZ7` in `history.py`).

### Keepalive
`_keepalive_loop()` in `main.py` self-pings `/health` every 4.5 minutes via the external Render URL to prevent spindown. GitHub Actions `.github/workflows/keep-alive.yml` pings every 5 minutes as backup.

---

## Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your credentials
uvicorn main:app --reload --port 10000
```

## Running Tests

```bash
pytest tests/
```

Tests are in `tests/` covering: `test_dedup.py`, `test_jira_handler.py`, `test_lark_handler.py`, `test_reconcile.py`, `test_utils.py`.

---

## Project History

See [`CHANGELOG.md`](CHANGELOG.md) for the full commit log grouped by date and feature.
