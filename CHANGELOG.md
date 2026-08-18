# Project Log — jira-lark-webhook

A reverse-chronological log of every commit on `main`, grouped by date.
Generated from `git log` on 2026-05-13.

Total commits: **41**
First commit: **2026-05-11** (`075fea5` — initial real-time bidirectional sync)
Latest commit: **2026-05-13** (`910c9cb` — split lark dedup key so writes don't silence deletes)

---

## 2026-08-18 — Deleting Timeline - Start/End in Lark never cleared the Jira date

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | fix | **Clearing `Timeline - Start`/`Timeline - End` on a Lark record left Jira's Start date/Due date untouched — reported live on VR-645..650 ("[Redesign] Organization/Member/Position/Group/Role and Permission/Login and Log out page"), all six still showing 2026-08-19..25 in Jira after the dates were deleted in Lark.** Captured the actual webhook payload from the user's delete via `GET /debug/payloads` (before_value had a real ms timestamp, after_value had `field_value: ""`) and confirmed live that Lark's Bitable API **omits** an empty field from `get_record`'s `fields` dict entirely rather than returning it as `null` (checked via `bitable_v1_appTableRecord_search` on the actual VR-645 record). Root cause: `lark_handler._handle_update_impl` decoded the clear correctly (`_decode_one` returns `(None, True)` for an empty raw value) but then gated the Jira write on `if start and start != ...` — a falsy `start` (genuinely cleared) was indistinguishable from "this field wasn't in the webhook at all," so the clear was silently dropped both on the fast (webhook-decode) path and the slow (`get_record`) path. Same conflation existed in reverse in `jira_handler._handle_update` (`if ts is not None and ...` for the `customfield_10015`/`duedate` changelog branches) — clearing a date in Jira never propagated to Lark either. Fix: gate on **presence**, not truthiness. `_handle_update_impl` now tracks whether `rec["fields"]` is a full `get_record` snapshot (in which a missing key is unambiguously "empty right now") vs. a partial fast-path decode (in which a missing key means "not this event" — the decode only includes keys for fields whose raw value actually changed, confirmed against the real webhook payload). Only writes the date (real value or `None`) when that distinction resolves to "we have an answer for this field this round." `jira_handler` needed no such presence tracking — a Jira changelog item only exists for fields that actually changed, so removing the `is not None` guard was sufficient there. `{"fields": {"duedate": null}}` clearing a Jira date field, and `None` clearing a Lark field via `update_record`, are both pre-existing, already-relied-upon patterns in this codebase (see the `F_MD`/`F_ASSIGNEE` "may be None to clear" writes) — not new API assumptions. **Data repair:** VR-645..650's stale `customfield_10015`/`duedate` cleared directly via the Jira API to match Lark's true (empty) state — confirmed VR-636 (the parent Epic) was never affected. Also flagged, not fixed here (no active custom lark_to_jira/jira_to_lark date mapping exists in the current defaults, so no live impact): the custom-field-mapping loops in both handlers have the identical truthy/is-not-None conflation for any dashboard-configured date field. 5 new regression tests: `test_update_clears_jira_start_date_fast_path`, `test_update_clears_jira_dates_full_snapshot`, `test_update_unrelated_field_change_does_not_clear_dates` (guards against the fast-path false-positive-clear risk — an unrelated field changing must not blank real dates) in `test_lark_handler.py`; `test_duedate_cleared_in_jira_clears_lark_end`, `test_startdate_cleared_in_jira_clears_lark_start` in `test_jira_handler.py`. Suite: 133 passed (128 baseline + 5). |

---

## 2026-08-14 — CRITICAL: `FieldNameNotFound` on every parent-touching write, and the dashboard field-rename that couldn't actually rename

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | fix | **Someone renamed the Lark-side parent-link column from "Parent items" to "Epic"; every write that touched a record's parent then failed with `FieldNameNotFound` and — because Lark's Bitable PUT is atomic per-request — silently dropped every OTHER field bundled into that same `update_record` call too (status, assignee, story points, actual/timeline dates, release).** Confirmed live via Render logs (`GET /v1/logs`, resource `srv-d80qbrd7vvec73eeuuhg`): 517 reconcile failures 03:13–09:18 across 259 distinct Lark records, plus 18 live-webhook failures from 09:32 across 11 Jira issues (VR-593/594/597/599/600/611/620/637/638/640/641). Verified collateral damage on 3 of those issues directly against both APIs: VR-593/594 (Jira "Ready to test") and VR-599 (Jira "Ready to test") all still showed their stale pre-change Lark status ("Ready to design" / "Developing") because the status write was bundled with the now-broken parent write in the same call. Root cause was NOT the hardcoded field name alone — the dashboard's `/settings/fields` already lets you edit a system mapping's `lark_field` (confirmed: editing "Parent items" → "Epic" via the config page updates `Supabase.field_mappings.id=6` in place, `PATCH .../field_mappings?id=eq.6` → 200), but nothing in the sync code ever read that value back. `jira_handler.py`, `reconcile.py`, and `lark_handler.py` imported `F_PARENT` (and every other system field name — `F_TITLE`, `F_START`, `F_END`, `F_ASSIGNEE`, `F_JIRA_KEY`, `F_JIRA_URL`, `F_TYPE`, `F_MD`, `F_RELEASE`, `F_ACTUAL_START`, `F_ACTUAL_END`, `F_JIRA_STATUS`) directly from `config.py` at import time — a value frozen for the life of the process, completely disconnected from `field_mappings.get_all()`/the Supabase cache. Live-tested this before touching code: triggered `POST /api/backfill` *after* the dashboard edit, watched fresh `FieldNameNotFound` errors keep firing at 14:51 — definitive proof the dashboard-only edit was cosmetic for system fields. **Fix: make the dashboard rename actually work, for every system field, not just a one-line hotfix for Parent/Epic.** `field_mappings.py` gains a stable-key lookup (`_SYSTEM_JIRA_KEYS`, keyed by each field's unchanging `jira_field` identity, e.g. `parent`/`summary`/`status`) behind a PEP 562 module `__getattr__` — `field_mappings.F_PARENT` now re-resolves the CURRENT `lark_field` from the live cache on every single access (not cached at the language level), falling back to `config.py`'s default only when the cache has no matching row. Every call site across `jira_handler.py`, `reconcile.py`, and `lark_handler.py` now reads `field_mappings.F_*` instead of importing a frozen `config.F_*` string (~90 call sites, mechanically requalified). `lark_api.update_record`'s cache-merge guard (which treats parent-link writes as non-round-trippable and invalidates rather than merges) now checks `field_mappings.F_PARENT` dynamically too, instead of a name registered once at `main.py` startup. Because `field_mappings.upsert()`/`update_lark_field()` already call `load()` to refresh `_cache`, a dashboard rename now takes effect on the very next sync — no redeploy, no restart. The already-diverged records self-heal too: `reconcile`'s 30-min sweep (and `/api/backfill`) recompute every field's Jira-vs-Lark diff from scratch each pass, so once the parent write succeeds, status/assignee/dates that were silently dropped alongside it get re-diffed and pushed on the very next pass — no separate data-repair script needed. No behavior change for the common case (no active dashboard rename) — all reads still resolve to the same default strings. Suite: 128 passed, no test changes required (tests mock literal Lark field-name strings in payload fixtures, independent of the constant-resolution mechanism). Migration `001_add_type_column.sql` (the separate, unrelated `sync_history.type` warning also seen today) is NOT part of this fix — it's a pre-existing, non-blocking, self-healing gap; applying it is optional and orthogonal. |

---

## 2026-06-08 — CRITICAL: kill the R. MD phantom-write quota drain (~93% of all Lark calls)

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | fix | **Reconcile re-wrote `R. MD` on every story-point-bearing record on every 6 h sweep — ~480 wasted `update_record`/day, the dominant Lark quota consumer.** Diagnosis was fully data-driven off `GET /debug/lark-calls` (the 2026-05-28 instrumentation): `update_record` was **5247 of 5653** total calls (93%), flat at ~490/day **including weekends** (May 30–31 Sat/Sun = 484/492, identical to weekdays → machine-driven, not user edits); `search_records` was **absent** (incremental reconcile never engages — the Main table has no ModifiedTime field — so all 4 daily reconciles are full sweeps); and the `sync_history` log showed only ~10 webhook events/day, so the writes weren't webhook-driven. Root cause, proven by dumping all 398 Main-table records (lark-cli, bot identity) + 200 Jira issues and replaying the exact `_sync_issue_to_lark` compare offline: **Lark's bitable v1 API returns Number fields as STRINGS** (`R. MD` → `"3"`, confirmed via raw `GET /bitable/v1/.../records/{id}`; `R. MD` is field type 2 = Number). The value-compare did `cur_md_num = cur_md if isinstance(cur_md, (int, float)) else None`, so the string `"3"` coerced to `None`, `3 != None` was always true, and reconcile rewrote `R. MD` every sweep for every record with story points (119 records have R. MD set; offline replay: 101 of 200 issues would phantom-write — ×4 sweeps/day ≈ ~480/day, matching the counter exactly). The write *succeeded* (wrote `3`, read back `"3"`) so **no data was corrupted and `retries_total` was 0** — pure wasted quota, invisible to the history log, which is why every earlier probe (dates round-trip fine; VR-156 fully converges) pointed elsewhere. Fix: parse the current Lark value through the existing `_sp_to_num()` so `"3" == 3` and the write is skipped when nothing changed. Applied at all four Lark-write compare sites: `reconcile._sync_issue_to_lark`, `reconcile.backfill`, and `jira_handler._handle_update` (the `customfield_10016` branch + the custom-number-mapping branch). Offline replay after fix: phantom `R. MD` writes **101 → 0**, genuine story-point changes still sync. The Lark→Jira number compare in `lark_handler` (line ~496) is left as-is — it compares against Jira's value, which returns proper numeric types, and writes to Jira not Lark. Expected effect: reconcile drops from ~490 → ~24 `update_record`/day (only legitimate drift), i.e. ~14.7k/month (over the 10k Basic cap) → <1k/month. **No data repair needed** — the bug wrote correct values, just redundantly. 2 regression tests use the real **string** number shape (`"R. MD": "5"`) that the live API returns and assert no write — `test_reconcile.test_no_phantom_write_when_lark_number_is_string` and `test_jira_handler.test_story_points_skipped_when_lark_value_is_string`; both fail on the pre-fix `isinstance` guard and pass after. (The pre-existing `R. MD` tests used an int and so never caught this — the same clean-shape blind spot, since lark-cli/test fixtures normalize numbers while the API returns strings.) Suite: 115 passed. Verify post-deploy: `GET /debug/lark-calls` `by_type.update_record` daily delta should fall from ~490 to low double digits. |

---

## 2026-05-28 — Per-call-type + retry instrumentation on the Lark counter

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | feat | **Measure-before-cut: break down Lark API usage by call type and count retries.** The `record_value` cache shipped 2026-05-25 didn't lower daily volume (still ~540/day real → ~16k/month, over the 10k Basic cap), and the global counter couldn't say WHICH calls dominate. It also under-reported by ~20%: `_request` incremented `_call_counts` once per logical call, before the retry loop, so 429-backoff retries (real HTTP calls Lark's admin counts) were invisible. Added `_classify(method, url)` mapping each Lark URL+method to a stable label (get_token / fetch_all_records / get_record / search_records / create_record / update_record / delete_record / list_fields / list_tables), and `_calls_by_type` + `_retries_by_type` counters incremented per actual HTTP attempt inside the retry loop (so totals now reconcile with the Lark console). `call_stats()` / `GET /debug/lark-calls` gains `by_type`, `retries_total`, `retries_by_type` (sorted desc). Existing `by_day` / `today` / `this_month` fields unchanged (backward compatible). No behavior change to sync — pure instrumentation to identify the dominant consumer (suspected: the reconcile loop's reads, 4×/day) before cutting. 4 new tests in `tests/test_lark_call_instrumentation.py`: classify maps all 9 URL patterns, attempt counted by type, retries counted by type (429→200), call_stats exposes breakdown. Suite: 113 passed |

---

## 2026-05-25 — Lark API call reduction via in-memory record cache

| Commit | Type | Summary |
|--------|------|---------|
| _pending_ | perf | **Eliminate `get_record` on the Jira→Lark update hot path** (the dominant remaining consumer of the Lark Basic 10k/month quota after `599c342`, `44ee5a3`, `672e4b4`). New `lark_api._record_cache` (5-min TTL) is populated as a free side-effect of every existing read path (`get_record`, `fetch_all_records`, `search_records_modified_since`) AND every write path (`update_record` merges fields, `delete_record` invalidates) AND the Lark→Jira webhook decode (`lark_handler._decode_after_value` now takes `record_id` and merges decoded fields). `jira_handler._handle_update:119` and decorative `_handle_delete:275` swap `get_record` for new `get_cached_or_fetch_record` (delegates to `get_record` on miss/expiry). Lark link fields (`F_PARENT`) don't round-trip between write-shape and read-shape, so they're registered as `_uncacheable_write_keys` and a write to those invalidates the cache entry instead of merging a wrong-shape value. Layer-1 kill switch (`lark_value_cache_enabled` setting + dashboard toggle + `POST /toggle/lark-value-cache`) bypasses cache reads without a redeploy. Table-switch handler also invalidates the entire cache (`main.py:272`, alongside `invalidate_fields_cache()`). 11 new tests in `tests/test_lark_value_cache.py` cover: cache-hit-skips-get_record, miss-falls-back-and-populates, TTL-expiry-refetches, VR-272 loop-guard regression preserved, update_record merges, uncacheable-key invalidates, webhook decode populates, fetch_all_records drift-repair, kill switch, invalidate_record_cache, delete invalidates. Existing `test_jira_handler.py` updated: 15 mock attributes renamed `get_record.return_value` → `get_cached_or_fetch_record.return_value`. Migration `002_add_lark_value_cache_setting.sql` seeds the kill-switch flag (default true). Suite: 109 passed |

---

## 2026-05-22 — Fix reconcile→webhook echo burst (no-op rewrites)

| Commit | Type | Summary |
|--------|------|---------|
| `4d02d3b` | fix | **Reconcile-triggered echo burst re-pushed unchanged custom values to Jira (2026-05-20 17:20:35→17:21:02 burst, ~17 records).** Lark sends a *full* record snapshot in both `before_value` and `after_value` on every `record_edited` webhook — only a few `field_value`s actually differ. `_decode_after_value` iterated `after_value` alone, so a webhook in which only Release changed handed downstream a `decoded` dict populated with EVERY relevant field's current value (Title, dates, Assignee, Release, Parent, P. QA md). The custom-mapping loop in `_handle_update_impl` then wrote each one to Jira **without value-comparing** — system fields were already compared, custom fields were not — so reconcile's legitimate Release/date writes echoed back as a wave of `"Sprint: Beta 1.2"` / `"QA Manday: 1.0"` history rows pushing the same values Jira already had. Fix layer 1 (H1, root cause): `_decode_after_value` now accepts `before_value` and only decodes fields whose raw value actually changed; `process()` plumbs `before_value` through `_handle_update` → `_handle_update_impl`. Fix layer 2 (H2, defense in depth): the custom-mapping loop now value-compares to current `jira_fields[...]` before adding to updates, matching what system fields already do. 4 regression tests: webhook with only-Release-changed doesn't re-push QA Manday; gate skips a custom value Jira already has; real changes still propagate; legacy `before_value=None` callers fall back to the old (unfiltered) decoder behavior. Data: burst was a noisy no-op — values written equalled Jira's current values, so nothing was corrupted, just rate-limit pressure |

## 2026-05-19 — CRITICAL: fix one-day date drift + runaway rewrite loop

| Commit | Type | Summary |
|--------|------|---------|
| `015b0d4` | fix | **Bidirectional date conflict ping-pong (VR-272): update path had no echo-suppression.** After the tz fix + restore, VR-272 was conflict-seeded (Jira held `06-15`/`06-22` from the changelog restore; Lark held a different value from a manual restore). The update sync path relies solely on value-comparison (TTL dedup was removed for updates), which cannot converge a concurrent bidirectional conflict — each handler reads stale opposite-side state and re-asserts its value, so `duedate` ping-ponged `2026-06-14 ↔ 2026-06-22` every ~3 s (Lark calls 389→566 before re-pause). Fix: added `dedup.date_echo_key` value-scoped echo suppression — when one update handler writes a date to the other side it marks the canonical `jira_key:slot:YYYY-MM-DD`; the mirrored handler skips re-propagating that exact echo, while a genuinely different new value still syncs (no legitimate edit dropped). Symmetric in `jira_handler` + `lark_handler`. Data: VR-272 Lark End repaired to `2026-06-22` (the corrupted side; Lark End `06-14` < Start `06-15`); all 244 linked records verified consistent (77 already agreed, 166 no-date, VR-272 fixed — 0 remaining conflicts). 7 regression tests cover both ping-pong directions, genuine-edit-not-suppressed, and a full 2-cycle converging. Suite: 93 passed |
| `faf6690` | fix | **Start/Due dates silently shifted −1 day on every reconcile, project-wide, and a non-converging value-compare drove a runaway Jira↔Lark rewrite loop.** `utils._jira_date_to_lark_ts` built a Bangkok-midnight timestamp (introduced 2026-05-18 by `50b6b7a`), but Bangkok midnight is `17:00Z the previous day` and Lark Bitable Date fields normalize to UTC-midnight — so Lark truncated every Jira→Lark date write down one calendar day, `_lark_ts_to_jira_date` read it back a day earlier, and that wrong value was written into Jira. Each reconcile/webhook cycle lost another day; because the Bangkok ts never equalled Lark's UTC-snapped value the loop guard never converged (225+ events, 402 Lark calls on 2026-05-19; ~50+ VR issues hit in the 11:55 +07 sweep, recurring since ~2026-05-13). Fix: `_jira_date_to_lark_ts` / `_lark_ts_to_jira_date` now use **UTC midnight** (matches Lark's storage) so the round-trip is exact, the value-compare is a no-op, and no day is lost. Removed the misleading `_BKK` constant from `utils.py`. 4 regression tests model Lark's UTC-day truncation and the loop-stability invariant (fail on the old `_BKK` code). Sync + reconcile paused during remediation; corrupted Jira+Lark dates restored from Jira changelog history |

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
