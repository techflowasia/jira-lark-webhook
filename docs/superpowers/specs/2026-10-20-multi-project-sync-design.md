# Multi-project sync — design

Date: 2026-10-20
Status: draft, awaiting approval

## Intent

Today the service syncs exactly one pair: one Jira project ↔ one Lark Base table. The user now has a second pair to sync — a different board / project key on the **same Jira site**, and a different Lark Base file in the **same Lark workspace**. They do not want to fork the repo or hardcode a second set of values.

Goal: one deployment syncs any number of **sync projects** (a Jira project ↔ Lark Base table pair). Each project is set up and behaves exactly like today's single sync. The dashboard makes it easy to **switch between and monitor** projects, and the audit (sync history) stays available.

## Goals

- Add a new sync project from the dashboard, with a guided setup — no code change, no env var, no redeploy.
- Each project independently owns everything that is per-pair today (see "What a project owns").
- Dashboard layout **C — sidebar + detail** (chosen by the user): a left sidebar listing every project with a health dot, "+ New project", and "All activity"; the right side is the selected project's dashboard (today's sections).
- Audit: each project page shows that project's history (as today); "All activity" shows a combined history with a project column and project filter.
- Per-project sync on/off and reconcile on/off, so a new project can be paused while being set up without touching the existing one.
- The current setup becomes project #1 automatically, with **no behaviour change and no data change**.

## Non-goals

- Multiple Jira sites or multiple Lark apps/tenants. Credentials (`JIRA_EMAIL/TOKEN/DOMAIN`, Lark app id/secret, Supabase) stay shared and global.
- Syncing one Jira project into two Bases, or two Jira projects into one Base table (one project = one Jira key ↔ one Base table; enforce uniqueness).
- User accounts / permissions on the dashboard.
- Redesigning the sync logic itself (handlers, decoders, echo suppression) beyond threading the project through.

## Anti-goals (failure even if it "works")

- Project #2 touching project #1's records, mappings, index, settings, or history — in any path (webhook, reconcile, backfill, setup).
- Any existing project #1 record being blanked, duplicated, re-created, or re-linked by the migration.
- A webhook being processed under the wrong project (e.g. a Lark event from Base B written using Base A's mappings).
- A misconfigured new project (missing column) producing failed writes in a loop, or blanking fields the user didn't intend to clear. Problems must surface in setup, not as data damage.
- Lark API call volume multiplying uncontrollably (the tenant quota was already blown once — see reconcile 6 h comment in `main.py`).

## Constraints

- Data Integrity Rule in `CLAUDE.md` applies: verify on deployed data on both sides, watch `/debug/lark-calls` and `/debug/payloads` for bounded call counts and no loops.
- Render free tier, single process, in-memory state rebuilt on startup.
- Supabase is the persistence layer; schema changes go in `migrations/` as SQL run manually.
- Lark per-Base QPS limits and the tenant monthly quota. Reconcile for N projects must be staggered, not simultaneous.

## What a project owns

Everything that is global module state today becomes per-project:

| Today (global) | Where | Becomes |
|---|---|---|
| `JIRA_PROJECT` env | `config.get_cfg()` | project.jira_project_key |
| Jira board (first board of the project) | `jira_api.get_board_id` | project.jira_board_id (optional; picked in setup since the user said "another board") — falls back to first board |
| `LARK_BASE_TOKEN` env + hardcoded in `main.py` | `get_cfg()`, `/auth/*` | project.lark_base_token |
| `LARK_TABLE_ID` env / `active_table_id` setting | `config`, `main.py` | project.lark_table_id (+ name) |
| field mappings | `field_mappings._cache`, Supabase `field_mappings` | rows scoped by `project_id` |
| `F_*` dynamic attrs | `field_mappings.__getattr__` | resolved per project |
| allowed sync types | `config._allowed_*_types`, `settings` | per project |
| assignee map | hardcoded `config.JIRA_TO_LARK_ASSIGNEE` | per project, editable in dashboard |
| `sync_enabled`, `reconcile_enabled` | `main.py` globals, `settings` | per project |
| in-memory index | `index._jira_to_lark/_lark_to_jira` | one index per project |
| dedup keys | `dedup._cache` | keys prefixed with project id |
| reconcile watermarks `last_reconcile_ts`, `last_full_reconcile_ts` | `reconcile._get_setting` | per project |
| Jira account-id / version / sprint caches | `lark_handler._*_cache` | per project |
| base timezone cache | `lark_api._base_tz_cache` (single slot) | keyed by base token |
| field-schema cache | `lark_api._fields_cache` | keyed by (base, table) |
| record value cache | `lark_api._record_cache` (by record_id) | keyed by (base, table, record_id) to be safe |
| history rows | `sync_history` | gain `project_id` column |
| update coalescing sets | `lark_handler._update_in_flight` | key by (project, rid) |

Stays global: `lark_value_cache_enabled` toggle, Lark tenant token, Jira credentials, keepalive, `/debug/lark-calls` (but add a per-project breakdown if cheap).

Key design fact: handlers, reconcile and `jira_api` already receive a `cfg` dict on every call. The project travels inside that dict (`cfg["PROJECT_ID"]` plus its keys); module-level state is looked up by project id. Prefer passing a project object/cfg explicitly over a "current project" global — the service handles webhooks for several projects concurrently, so a mutable global "active project" would be a cross-project corruption bug.

## Data model (Supabase)

New table `sync_projects`:
- `id` (pk), `name` (display name, defaults to Jira key), `jira_project_key`, `jira_board_id` (nullable), `lark_base_token`, `lark_table_id`, `lark_table_name`, `sync_enabled` (bool, default **false** for new projects), `reconcile_enabled` (bool, default false for new), `allowed_jira_types` (json), `allowed_lark_types` (json), `assignee_map` (json: `{jira_display_name: lark_name}`), `setup_state` (e.g. `draft` / `connected` / `active`), `created_at`.
- Unique on `jira_project_key`; unique on (`lark_base_token`, `lark_table_id`).
- Supabase schema already allows this: `field_mappings` rows are what decide what syncs (see "The mapping table decides what syncs").

`field_mappings`: add `project_id` (fk). `sync_history`: add `project_id` (nullable — system/global events may have none). Per-project watermarks: either columns on `sync_projects` or `settings` keys suffixed with the project id — builder's call.

Migration `003_multi_project.sql`:
1. Create `sync_projects`.
2. Insert project #1 from today's live values: `JIRA_PROJECT` env, the Base token currently hardcoded in `main.py`, `active_table_id/name` from `settings` (falling back to `LARK_TABLE_ID` env), current `sync_enabled` / `reconcile_enabled` / allowed types from `settings`, assignee map from `config.py`.
3. Backfill `field_mappings.project_id` and `sync_history.project_id` with project #1's id.
4. Copy reconcile watermarks to project #1's.

The migration must be idempotent and must not modify any Lark or Jira record.

## Webhook routing

- **Jira** (`/webhook/jira`, single URL for all projects): route by `issue.fields.project.key` (fallback: prefix of `issue.key`). No matching project, or project with sync disabled → log and ignore. For `jira:issue_deleted` the payload still carries the issue, so the same rule works.
- **Lark event subscription** (`/webhook/lark`): route by the event's Base token (`file_token`) and `table_id`. Verified on the deployed service: project #1 receives `drive.file.bitable_record_changed_v1` events whose `event` carries `file_token` (the Base token) and `table_id`. Project #1 uses event subscription (no Automation payloads in the recent `/debug/payloads`), and new projects use the same path. Unknown base/table → ignore and log (today it ignores non-configured tables in `lark_handler.process`; keep that guard per project).
- **Lark Automation** (`/webhook/lark-auto`): payload only has `table_id`. Builder's call between (a) a per-project URL `/webhook/lark-auto/{project_id}` shown in the setup UI, or (b) requiring `base_token` in the body. Whichever is chosen, an unroutable call must be ignored, not defaulted to project #1. Keep the existing URL working for project #1 for backward compatibility.
- Cross-project links (a Jira parent in another project, etc.) are out of scope: treat as unresolvable, same as today's behaviour for unknown parents.

## New-project setup flow (dashboard "+ New project")

A wizard on the right-hand pane. The project is saved as a `draft` with sync and reconcile **off** from the first step, so a half-finished setup never syncs.

1. **Name + Jira**: enter the Jira project key. Service checks the key exists and is reachable with the shared credentials, lists the project's boards (pick one, default first), and lists its issue types.
2. **Lark**: paste the Base link (extract the base token from the URL). Service lists tables; pick one. If the bot can't read the Base yet, show the "Connect Lark" step first.
3. **Connect Lark**: the existing `/auth/start` → `/auth/callback` flow, parameterised by project (pass the project id through OAuth `state`), which adds the bot as editor and subscribes to record-change events **for that project's Base**. Show the `/webhook/lark-auto/...` URL too, as today's alternative.
4. **Copy from**: choose an existing project to copy field mappings, sync types and people from (user said the new Base has similar fields, possibly fewer columns, and a partly-overlapping team).
5. **Check fields**: compare each copied mapping's Lark column against the new table's actual columns (and Jira custom fields against the Jira field list). Mappings whose column is missing are flagged; the user must drop or re-point each before continuing. Also warn about the `Jira Key` column specifically — without it, linking and the index can't work.
6. **People**: show the copied assignee map plus suggestions — Jira users assignable in the new project and the options of the new Base's Assignee field. User keeps shared faces, removes absent ones, adds new people.
7. **Sync types**: pick allowed Jira / Lark types (same UI as today).
8. **Backfill**: run the existing backfill for this project to link/create records, show the result summary (same as today's dashboard backfill).
9. **Go live**: turn sync on (and reconcile on, if wanted). Reminder shown: the Jira admin webhook must include this project — if it's filtered by JQL to one project, widen it (cannot be done from this app).

Every field shown in setup stays editable later on the project's page.

## The mapping table decides what syncs

User decision: a project's field-mapping table is the complete list of what syncs. A field with no active mapping row in that project **never syncs, in any path** (webhook create/update/delete, reconcile, backfill) — including the built-in ("system") fields. If a project maps only 4 fields, only those 4 sync. The user adds or deletes mappings to change this.

Consequences for the builder:
- `field_mappings.__getattr__` must stop falling back to the `config.py` default name when the project has no row for a system field; it returns "not mapped" and every read/write of that field is skipped. Today every handler and reconcile path writes `F_START`, `F_RELEASE`, `F_MD`, `F_ACTUAL_*`, `F_JIRA_STATUS`, `F_PARENT` unconditionally — each needs a skip-if-unmapped check (this is the main sync-logic change in this project).
- Direction on a mapping row is honoured as today.
- Required for a project to go live (my call — the link and the type filter cannot work without them): Title (`summary`), Jira Key (`key`), Type (`issuetype`). Setup and the mapping editor block deleting these.
- Setup step 5 then becomes: copied mappings whose Lark column is missing in the new table are flagged; the user re-points or deletes them. Deleting = that field doesn't sync in this project.
- Project #1: its mapping rows all exist in Supabase today, so nothing changes unless the user deletes one. Builder must verify project #1's rows cover every system field before the fallback is removed (if Supabase is missing any row that today works via the fallback, the migration inserts it, so project #1's behaviour is unchanged).

## Unmapped and unassigned people

User decision (my reading of "option 2, clears Lark" — to confirm): a Jira assignee who is unassigned **or** not in the project's people list → the Lark Assignee is **cleared**, as `jira_handler._handle_update` does today. Additionally, an unmapped person writes a warning row in the project's history naming the person, and the project page shows a count ("2 people not mapped"), so a missing people-list entry is visible instead of silently blanking.

Builder: today reconcile (`reconcile.py` ~239 / ~517) leaves Lark unchanged for unmapped people while the update handler clears it. Make the paths consistent with the rule above. Lark → Jira: a Lark assignee with no Jira mapping leaves Jira unchanged + warning (today's behaviour; Jira can't be given an unknown person).

## Dashboard (layout C)

- **Sidebar**: one row per project — name, Jira key, health dot: 🟢 sync on and no errors in 24 h; 🔴 errors in last 24 h; ⏸ sync off; ⚙ draft/setup incomplete. Then "+ New project", then "All activity".
- **Project page** (`/p/{project_id}` or `/?project=`): today's dashboard scoped to the project — status cards, sync / reconcile toggles, table picker, field mappings, sync types, people (new section), backfill, history with today's filters.
- **All activity**: today's history table with a Project column and a project filter, same range/search/pagination.
- `/` opens the last-viewed project (or the first). The global Lark value cache toggle and debug links live outside the project scope (e.g. sidebar footer).
- All existing API endpoints become project-scoped (`/api/p/{id}/fields`, …) or take a `project_id`. Old unscoped endpoints may remain as aliases for project #1 during transition — builder's call.

## Reconcile

One loop, iterates enabled projects sequentially every 6 h (same cadence), each with its own watermarks. User: API call volume is not a concern for this change — no per-project call budget needed; keep the `/debug/lark-calls` monitoring as today. Lark/Jira errors in one project must not abort the others. The >10-deletion safety guard is per project.

## Rollout / verification (required for "done")

1. Migration on Supabase; deploy; confirm project #1 loads with identical mappings, types, people, toggles, and its index count matches pre-deploy `/debug/index`.
2. Watch project #1 for a period: real edits both ways still sync; `/debug/lark-calls` stays at the pre-deploy rate.
3. Create project #2 via the wizard; keep sync off through backfill; sample records on both sides of project #2 for correctness.
4. Turn project #2 on; edit records in both projects; confirm each lands only in its own pair, history rows carry the right project, and call counts stay bounded.
5. Tests: existing suite green; new tests for routing (Jira by project key, Lark by base/table, unknown → ignored), per-project index/dedup isolation (same-looking keys never cross), migration idempotency, and the field-check step.

## Decided by the user

- Same Jira site, different project key / board; same Lark workspace, different Base file.
- Fields, people and issue types are similar; the new Base may have fewer columns; the team partly overlaps.
- Dashboard layout C (sidebar + detail). Audit must remain available.
- Per-project sync and reconcile toggles.
- The mapping table is the whole list of what syncs; unmapped fields never sync (built-ins included).
- Unassigned/unmapped Jira assignee clears Lark (reading to confirm).
- API call volume is not a concern; expected a handful of projects.
- Each project works like today's sync; the change is about setup, switching and monitoring.

## My calls (flagged, user can override)

- New projects start with sync and reconcile **off** until "Go live".
- Copy-from-existing-project as the setup starting point.
- Title, Jira Key, Type are required mappings that can't be deleted.
- Unmapped person also logs a warning and shows a count on the project page.
- Jira routing by issue project key on the single existing webhook URL.
- Credentials stay global; the Lark app secret currently hardcoded in `main.py` should move to the env var that already exists (`LARK_APP_SECRET`) while touching that code.

## Left to the builder

- Exact Supabase schema details (watermark storage, json vs separate tables for assignee map).
- How module-level caches are restructured (dict keyed by project id vs a per-project context object).
- `/webhook/lark-auto` routing option (a) or (b) above.
- URL scheme for project pages and APIs, and whether to keep unscoped aliases.
- Wizard UI details, within the existing server-rendered HTML style of `main.py`.
