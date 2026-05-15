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
| `field_mappings.py` | Configurable field mapping cache — loads from Supabase, falls back to hardcoded defaults |
| `utils.py` | Field parse helpers: `_lark_text`, `_lark_select`, date/timestamp converters |
| `migrations/` | One-off SQL migrations for the Supabase schema (run manually in the SQL editor) |
| `tests/` | pytest suite: `test_dedup.py`, `test_jira_handler.py`, `test_lark_handler.py`, `test_reconcile.py`, `test_utils.py` |

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
| `GET` | `/auth/start` | Lark OAuth flow start |
| `GET` | `/auth/callback` | Lark OAuth callback (adds bot as editor + subscribes to events) |

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

Hardcoded in `main.py` (not secrets — Lark app credentials for the OAuth flow):
- `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_BASE_TOKEN`, `BOT_OPEN_ID`, `REDIRECT_URI`

---

## Supabase Tables

| Table | Purpose |
|-------|---------|
| `sync_history` | Event log (direction, event, jira_key, lark_id, type, description, status, error, ts). The `type` column (Epic / Story / Task / …) is added by `migrations/001_add_type_column.sql`. |
| `field_mappings` | Configurable Lark↔Jira field mappings |
| `settings` | Key-value store: `sync_enabled`, `reconcile_enabled`, `active_table_id`, `active_table_name`, `allowed_jira_types`, `allowed_lark_types` |

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

**Sprint field (customfield_10020) is read-only via Agile API** — skipped in issue update loop; moved via `jira_api.move_to_sprint()`.

---

## Assignee Mapping (hardcoded in `config.py`)

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
