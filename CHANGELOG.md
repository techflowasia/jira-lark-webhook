# Project Log — jira-lark-webhook

A reverse-chronological log of every commit on `main`, grouped by date.
Generated from `git log` on 2026-05-12.

Total commits: **39**
First commit: **2026-05-11** (`075fea5` — initial real-time bidirectional sync)
Latest commit: **2026-05-12** (`e9337fc` — Type column in sync history)

---

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
