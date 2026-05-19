# Project Log — jira-lark-webhook

A reverse-chronological log of every commit on `main`, grouped by date.
Generated from `git log` on 2026-05-13.

Total commits: **41**
First commit: **2026-05-11** (`075fea5` — initial real-time bidirectional sync)
Latest commit: **2026-05-13** (`910c9cb` — split lark dedup key so writes don't silence deletes)

---

## 2026-05-19 — CRITICAL: fix one-day date drift + runaway rewrite loop

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | fix | **Start/Due dates silently shifted −1 day on every reconcile, project-wide, and a non-converging value-compare drove a runaway Jira↔Lark rewrite loop.** `utils._jira_date_to_lark_ts` built a Bangkok-midnight timestamp (introduced 2026-05-18 by `50b6b7a`), but Bangkok midnight is `17:00Z the previous day` and Lark Bitable Date fields normalize to UTC-midnight — so Lark truncated every Jira→Lark date write down one calendar day, `_lark_ts_to_jira_date` read it back a day earlier, and that wrong value was written into Jira. Each reconcile/webhook cycle lost another day; because the Bangkok ts never equalled Lark's UTC-snapped value the loop guard never converged (225+ events, 402 Lark calls on 2026-05-19; ~50+ VR issues hit in the 11:55 +07 sweep, recurring since ~2026-05-13). Fix: `_jira_date_to_lark_ts` / `_lark_ts_to_jira_date` now use **UTC midnight** (matches Lark's storage) so the round-trip is exact, the value-compare is a no-op, and no day is lost. Removed the misleading `_BKK` constant from `utils.py`. 4 regression tests model Lark's UTC-day truncation and the loop-stability invariant (fail on the old `_BKK` code). Sync + reconcile paused during remediation; corrupted Jira+Lark dates restored from Jira changelog history |

## 2026-05-18 — Fix Jira→Lark parent + start/end date sync (silent drops)

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | fix | **Stale Lark Release moved Jira cards to the wrong sprint.** Release/sprint synced Jira→Lark only inside the `customfield_10020` changelog branch (fired only on a sprint change) and `reconcile._sync_issue_to_lark` didn't reconcile it at all — so a non-sprint Jira edit (sub-task add, man-day change) left a diverged Lark Release, and the Lark→Jira `move_to_sprint` path then pushed that stale value back, moving the Jira card to the wrong sprint (VR-256/258: Jira Beta 1.3, Lark Beta 1.4 → card moved to 1.4). Now Release is reconciled from `issue.fields.customfield_10020` (Jira's current sprint, source of truth) on **every** update — mirroring the parent block — plus in `reconcile._sync_issue_to_lark` and its create path; set-compared so it can't loop. Removed the now-dead `_split_sprint_changelog` |
| `fedf50c` | fix | **Custom Jira→Lark mapping silently dropped + Number write failed.** Same gate root cause: a dashboard-configured custom field (e.g. `customfield_10178` "QA Man day") isn't in `RELEVANT_CHANGELOG_FIELDS`, so a changelog containing only that field hit the line-113 early-return — no sync, no log. The gate now also passes for any field in the active custom Jira→Lark mappings. The custom loop also wrote the raw changelog string to the Lark field, so a Number field failed with `NumberFieldConvFail`; it now coerces by `field_type` (number via `_sp_to_num`, date via `_jira_date_to_lark_ts`, else text) and value-compares to avoid redundant writes/loops |
| `50b6b7a` | fix | **Parent change never synced, no log.** Jira fires the parent-change changelog as field `IssueParentAssociation` (fieldId `None`), not `parent`. `RELEVANT_CHANGELOG_FIELDS` only had `parent`, so the line-103 gate returned early — no fetch, no parent reconcile, no history row. Added `IssueParentAssociation` to the gate set; parent resolver now falls back to the changelog `toString` when `issue.fields.parent` is absent; and a parent change whose target isn't linked in Lark yet records a `skipped` history row instead of a silent no-op (Data Integrity Rule — no hidden divergence). Confirmed from the live VR-331 webhook |
| `50b6b7a` | fix | **Start/End date never synced Jira→Lark, no log.** `customfield_10015` (Start date) and `duedate` (End date) were absent from `RELEVANT_CHANGELOG_FIELDS` and had no changelog branch, and were never set on create or in reconcile — the `both`-direction mapping only ever ran Lark→Jira. Added both to the gate set + changelog branches (use the clean `to` value), and to the create path, `reconcile._sync_issue_to_lark`, and backfill Steps 3+5. Fixed the dead `utils._jira_date_to_lark_ts` to use Bangkok midnight (`_BKK`) so it round-trips exactly with `_lark_ts_to_jira_date` — a naive/UTC midnight would make every date write differ from Lark's stored value (redundant writes / sync loop) |

## 2026-05-16 — Project rule: data-integrity verification

| Commit | Type | Summary |
|--------|------|---------|
| `812eddb` | docs | Add "Data Integrity Rule" to `CLAUDE.md`: every requirement/bugfix must verify real Jira↔Lark data isn't lost/diverged and repair data the bug already corrupted (sample deployed records both sides, watch `/debug/lark-calls` for loops) — tests passing alone is not "done" |

## 2026-05-15 — Fix Title sync loop + multi-select Release corruption

| Commit | Type | Summary |
|--------|------|---------|
| `1cc808f` | fix | **Title sync loop (VR-227).** The `record_edited` webhook delivers text fields as a JSON-*stringified* rich-text array (`'[{"text":"hi","type":"text"}]'`), but `lark_handler._decode_one` returned that string verbatim for text fields, so `_lark_text` yielded the raw JSON. That JSON went to Jira summary → synced back to Lark Title → each round added another `[{"type":"text",...}]` layer (exponential nesting, runaway API burn). Decoder now `json.loads`-parses text values to the list shape `get_record` returns; unparseable values fall back to `get_record`. Regression: `test_decode_one_text_parses_json_stringified_array`, `test_fast_path_title_writes_plain_text_not_json` |
| `1cc808f` | fix | **Multi-select Release combined-option corruption.** Jira's sprint changelog `toString` is comma-joined (`"VR Sprint 2, Beta 1"`); `jira_handler` wrote `[to_str]` so Lark's multi-select Release created a single bogus combined option. Now split into separate option names (`_split_sprint_changelog`), use all sprint names on the create path (`_sprint_names`), and compare as a set via new `utils._lark_multi` so a multi-value Release no longer triggers redundant writes. Same fix in `reconcile.py` backfill Step 5. Regression: `test_sprint_changelog_splits_into_separate_release_options`, `test_sprint_changelog_skips_when_release_set_matches` |

| Commit | Type | Summary |
|--------|------|---------|
| `fad84a5` | feat | `GET /debug/lark-calls` — per-day Lark API call counter (in `lark_api._request`, Bangkok day boundary) exposing today / this_month / total / by_day. Lets us verify the quota-reduction work landing; Lark console stays authoritative for the real monthly figure |
| `44ee5a3` | perf | Two-tier reconcile: a full sweep at most once per 24 h (unchanged logic — still removes duplicates and deletes orphans), with lightweight incremental runs in between that fetch only Lark records modified since the last run (`records/search` filtered on the auto-detected "Last modified time" field) + Jira issues with `updated >=` the last run. Auto-detects the modified-time field by `ui_type`/type 1002; if absent, no prior timestamp, a stale (>24 h) full sweep, or any incremental error, it safely falls back to the original full reconcile. `last_reconcile_ts`/`last_full_reconcile_ts` persisted in Supabase `settings`. Cuts reconcile's monthly Lark calls from ~21k to a few hundred without weakening the missed-webhook safety net |
| `7a0fa10` | fix | Sprint↔Release sync silently skipped for newly created Jira sprints/versions. Two root causes: (1) `_get_sprint_map`/`_get_version_map` cached the Jira name→id maps for 1 h, so a sprint/version created within that window wasn't in the map and the lookup returned None — added `_resolve_id` refresh-on-miss (throttled to 1 forced Jira refetch per 60 s); (2) the `move_to_sprint` call sat after `if not updates: return`, so a Release-only change that maps to a sprint but not a fixVersion never reached it — sprint resolution now runs before the guard, `update_issue` only fires when there are field updates, and the move is skipped if the issue is already in that sprint |
| `599c342` | perf | Skip `get_record` on Lark→Jira updates by decoding the changed fields straight from the `record_edited` webhook's `after_value` payload. New `lark_api.get_field_meta_by_id` (reuses the 60 s field cache) + `lark_handler._decode_after_value` translate `{field_id, field_value}` into the same shape `get_record` returns (text/number/select/multiselect/date/link), filtered to fields the update path actually syncs. Falls back to `get_record` for auto-discover, missing payloads, unknown fields, or undecodable values. Eliminates ~3 Lark calls per Lark edit |
| _pending_ | chore | gitignore `.gstack/` (local QA/skill artifacts — QA report for this work lives at `.gstack/qa-reports/`, not tracked) |
| `672e4b4` | perf | Stretch the reconcile loop from every 30 min to every 6 h. At 30 min it alone consumed ~21k Lark API calls/month and was the primary cause of the tenant exhausting its 10k/month Basic API quota (HTTP 429 / code 99991403). Reconcile is a missed-webhook safety net, not the real-time path; the dashboard "Run Backfill" button still forces an immediate full reconcile |

## 2026-05-14 — Lark 429 retry/backoff + field-schema cache + update coalescing

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | fix | Make app startup resilient to Lark 429s — initial index rebuild now runs as a background task and swallows failures, so Render's port-bind scan can't be killed by a Lark outage. Empty index is repaired by the 30-min reconcile and the auto-discover path in `_handle_update` |
| _pending_ | fix | Coalesce concurrent `record_edited` events for the same `rid` in `lark_handler._handle_update` (per-rid in-flight set + pending re-run flag) — Lark frequently fires duplicate edited events that previously raced on `get_record` and tripped per-Base QPS limits, surfacing as 429 errors in the history log even on the first apparent webhook |
| _pending_ | fix | 60 s TTL cache for `lark_api.list_fields` / `get_select_options` so the dashboard field-mapping dropdown stops getting stuck on "Loading…" when Lark returns 429 on repeat page loads; invalidated when the active table is switched |
| _pending_ | fix | Add retry/backoff (Retry-After aware) to all `lark_api` HTTP calls so bursts of `record_edited` webhooks no longer flood the dashboard with 429 "Too Many Requests" errors; stop re-calling Lark from the `lark_handler.process` catch-block when the original failure was itself a Lark API error |

## 2026-05-13 — Delete cascade + duplicate-create race fixes

| Commit | Type | Summary |
|--------|------|---------|
| `910c9cb` | fix | Split lark dedup key (`lark:` for writes, `lark_delete:` for deletes) so a write within 120 s no longer silently swallows a user-initiated delete |
| `fc67a95` | fix | Per-rid in-flight lock in `lark_handler._handle_create` to prevent duplicate Jira issues from parallel webhook deliveries; reorder dedup+index mark to before Lark write-back; cascade Lark delete → Jira delete (was preserve-only); add matching `dedup.is_ours("jira:")` skip in `jira_handler._handle_delete` |

## 2026-05-12 — Hardening, loop-prevention, and dashboard polish

| Commit | Type | Summary |
|--------|------|---------|
| `e9337fc` | fix | Populate Type column in sync history dashboard |
| `367f5ac` | fix | Route unlinked `record_edited` to create handler so new Lark rows sync |
| `321b093` | fix | Send story points as number, not string (Lark `NumberFieldConvFail`) |
| `cdf06dd` | test | Update update-handler tests for value-comparison loop prevention |
| `6dc60fa` | feat | Add issue-type column to sync history; fix subtask parent not syncing on title-only edits |
| `4cca41a` | fix | Replace TTL dedup lock with value comparison to prevent false sync drops |
| `2f1fee4` | feat | Sync Release and Parent item in backfill Step 5 |
| `20ca587` | fix | Skip `customfield_10020` in custom mapping loop — sprint requires Agile API not issue update |
| `f319e34` | fix | Include Jira error body in 400 exceptions; clean up lark→jira error logs |
| `11b5d27` | fix | Send sprint→Release as array (multi-select field requires list) |
| `61493a1` | feat | Sprint→Release sync, fieldId fix, reconcile toggle, UTC+7 timestamps |
| `cb4ccac` | fix | Resolve parent Jira key from issue fields instead of changelog `toString` |
| `2e177ec` | feat | Snapshot title before every delete and include in log |
| `f5ceaf0` | fix | Title-match dedup + mobile responsive UI |
| `f9b5fc0` | fix | Pick correct duplicate Lark record by title match against Jira summary |
| `2a9745a` | fix | Reconcile dedup + full bidirectional backfill button |
| `8984ea2` | feat | Match unlinked records by title + dynamic JQL for reconcile |
| `d09c33e` | feat | Dynamic sync types config — dashboard UI to edit allowed Jira/Lark types |
| `ede71f7` | fix | Keepalive must ping external URL, not localhost |
| `8d4ce22` | feat | All field mappings are now fully editable and deletable |
| `4bbeb6e` | fix | Import `jira_api` in `main.py` for `/api/jira-fields` endpoint |
| `b9ab80b` | feat | Field mapping edit uses dropdowns from live Lark/Jira field lists |

## 2026-05-11 — Bootstrap → first production-ready release

| Commit | Type | Summary |
|--------|------|---------|
| `c773a43` | fix(qa) | ISSUE-002,003 — two more JS parse errors from backslash-quote in f-string |
| `75c50b5` | fix(qa) | ISSUE-001 — JS parse error from unescaped quotes in `switchTable` onclick |
| `5e75b0c` | feat | Add field mapping UI + table selector + non-blocking table switch |
| `3c65057` | fix | Load active table from Supabase in background task, not at startup |
| `efb0236` | feat | Dynamic Lark table selector in dashboard |
| `7bb8a92` | fix | Use Bangkok (UTC+7) for Lark timestamp → Jira date conversion |
| `55030c8` | feat | Persistent history (Supabase), self-ping keep-alive, dashboard filters |
| `752afdf` | fix | Use `utcfromtimestamp` for Lark date → Jira date conversion |
| `be5fc21` | feat | Add Lark file subscribe call in OAuth callback |
| `4edbd76` | fix | Use `app_access_token` Bearer for Lark user token exchange |
| `0254c05` | feat | OAuth flow to add bot as Base editor via user token |
| `9e37e49` | feat | Add `/webhook/lark-auto` endpoint for Lark Base Automation trigger |
| `7b37e77` | fix | Undefined `f` variable in `jira_handler` + auto-discover Jira key in lark update |
| `1cde92b` | feat | Enable/disable sync toggle on dashboard |
| `912ef6d` | feat | Dashboard UI + history log + raw payload debug endpoint |
| `2e53ad1` | ci | GitHub Actions keep-alive ping every 10 min |
| `075fea5` | feat | Real-time bidirectional Jira ↔ Lark webhook sync (initial commit) |

---

## Themes

### Real-time sync engine
The core webhook plumbing was built in a single day (2026-05-11) — initial bidirectional sync (`075fea5`), Lark Automation webhook variant (`9e37e49`), and OAuth-driven bot installation (`0254c05`, `4edbd76`, `be5fc21`).

### Dashboard surface
Dashboard, history log, and debug endpoints landed early (`912ef6d`) and grew quickly: sync toggle (`1cde92b`), dynamic table selector (`efb0236`), live-field-driven mapping editor (`b9ab80b`, `8d4ce22`), and configurable sync types (`d09c33e`). Mobile responsive pass in `f5ceaf0`.

### Loop prevention evolution
1. **TTL dedup** — initial (`dedup.py`, 120 s window).
2. **Title-match dedup** — `f5ceaf0`, `f9b5fc0` handle duplicate Lark rows that share a Jira key.
3. **Value comparison** — `4cca41a` replaces TTL-only update guard with per-field value diffing in `jira_handler._handle_update` / `lark_handler._handle_update`. The TTL cache remains for `create` and `delete` paths.

### Reconcile & backfill
Periodic reconcile and one-shot backfill (`2a9745a`) became the safety net for missed webhooks. Backfill was later extended to sync Release and Parent (`2f1fee4`); reconcile gained dynamic JQL and title-match for unlinked rows (`8984ea2`) and a runtime enable/disable toggle (`61493a1`).

### Field-mapping quirks discovered the hard way
- Sprint (`customfield_10020`) is read-only via the issue REST API — must use Agile API (`20ca587`).
- `Release` is a multi-select — values must be sent as an array (`11b5d27`).
- Story points must be sent as a number, not a string, or Lark returns `NumberFieldConvFail` (`321b093`).
- Lark date timestamps are millisecond UTC — convert via Bangkok (UTC+7) for correct calendar dates (`7bb8a92`, `752afdf`).
- Parent key resolution must read `fields.parent.key`, not the changelog's `toString` which holds the summary (`cb4ccac`).

### Keepalive
Render free tier spins down after ~15 min idle. Two redundant pings: in-process self-ping (`55030c8`, corrected in `ede71f7` to use the external URL) and a GitHub Actions cron every 10 min (`2e53ad1`).

---

_To regenerate this log:_
```bash
git log --pretty=format:"%h|%ad|%s" --date=short
```
