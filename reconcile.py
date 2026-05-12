"""30-min safety-net cron: full diff Jira <-> Lark."""
import logging
from collections import Counter
import lark_api, jira_api, index, dedup, config
from config import (F_TITLE, F_JIRA_KEY, F_JIRA_URL, F_TYPE, F_ASSIGNEE,
                    F_MD, F_JIRA_STATUS, F_ACTUAL_START, F_ACTUAL_END,
                    F_PARENT, F_START, F_END,
                    JIRA_TO_LARK_ASSIGNEE, LARK_TO_JIRA_ASSIGNEE)
from utils import _lark_text, _lark_select, _jira_datetime_to_lark_ts, _lark_ts_to_jira_date

log = logging.getLogger(__name__)


def run(cfg: dict) -> None:
    log.info("Reconcile: starting")
    try:
        token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
        lark_records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
        jira_issues = jira_api.fetch_all_issues(cfg, types=list(config.get_allowed_jira_types()))
    except Exception as e:
        log.error(f"Reconcile: fetch failed — {e}")
        return

    jira_keys  = {i["key"] for i in jira_issues}
    jira_by_key = {i["key"]: i for i in jira_issues}

    # Group linked Lark records by Jira Key, resolve duplicates by title match
    lark_grouped: dict = {}
    for rec in lark_records:
        jk = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if jk:
            lark_grouped.setdefault(jk, []).append(rec)

    lark_by_jira_key: dict = {}
    for jk, recs in lark_grouped.items():
        if len(recs) == 1:
            lark_by_jira_key[jk] = recs[0]
            continue
        # Keep the record whose title matches the Jira summary; fallback to first
        jira_summary = (jira_by_key.get(jk, {}).get("fields", {}) or {}).get("summary", "") or ""
        keeper = next((r for r in recs if _lark_text(r["fields"].get(F_TITLE)) == jira_summary), recs[0])
        lark_by_jira_key[jk] = keeper
        for rec in recs:
            if rec["record_id"] == keeper["record_id"]:
                continue
            rid = rec["record_id"]
            dedup.mark(f"lark:{rid}")
            try:
                lark_api.delete_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
                log.warning(f"Reconcile: deleted duplicate Lark {rid} (kept {keeper['record_id']} for {jk})")
            except Exception as e:
                log.error(f"Reconcile: delete duplicate {rid}: {e}")

    index.rebuild(lark_records)

    # Delete Lark records for Jira issues that no longer exist
    to_delete = [(jk, rec) for jk, rec in lark_by_jira_key.items() if jk not in jira_keys]
    if len(to_delete) > 10:
        log.error(f"Reconcile: safety guard — {len(to_delete)} deletions blocked")
    else:
        for jk, rec in to_delete:
            rid = rec["record_id"]
            dedup.mark(f"lark:{rid}")
            try:
                lark_api.delete_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
                index.remove_by_jira(jk)
                log.info(f"Reconcile: deleted Lark {rid} (Jira {jk} gone)")
            except Exception as e:
                log.error(f"Reconcile: delete {rid}: {e}")

    # Create / update Lark records for all Jira issues
    for issue in jira_issues:
        key = issue["key"]
        jf = issue["fields"]
        itype = jf["issuetype"]["name"]
        if itype not in config.get_allowed_jira_types():
            continue

        assignee_name = (jf.get("assignee") or {}).get("displayName")
        lark_assignee = JIRA_TO_LARK_ASSIGNEE.get(assignee_name) if assignee_name else None
        sp = jf.get("customfield_10016")
        sp_str = _sp_to_str(sp)
        jira_status = (jf.get("status") or {}).get("name")
        actual_start = _jira_datetime_to_lark_ts(jf.get("customfield_10175"))
        actual_end = _jira_datetime_to_lark_ts(jf.get("customfield_10176"))

        if key in lark_by_jira_key:
            rec = lark_by_jira_key[key]
            rid = rec["record_id"]
            updates = {}

            if lark_assignee and _lark_select(rec["fields"].get(F_ASSIGNEE)) != lark_assignee:
                updates[F_ASSIGNEE] = [lark_assignee]
            if sp_str is not None and str(rec["fields"].get(F_MD) or "") != sp_str:
                updates[F_MD] = sp_str
            if jira_status and _lark_text(rec["fields"].get(F_JIRA_STATUS)) != jira_status:
                updates[F_JIRA_STATUS] = jira_status
            if actual_start is not None and rec["fields"].get(F_ACTUAL_START) != actual_start:
                updates[F_ACTUAL_START] = actual_start
            if actual_end is not None and rec["fields"].get(F_ACTUAL_END) != actual_end:
                updates[F_ACTUAL_END] = actual_end

            if updates:
                dedup.mark(f"lark:{rid}")
                try:
                    lark_api.update_record(token, cfg["LARK_BASE_TOKEN"],
                                           cfg["LARK_TABLE_ID"], rid, updates)
                    log.info(f"Reconcile: updated Lark {rid} ({key})")
                except Exception as e:
                    log.error(f"Reconcile: update {rid}: {e}")
        else:
            fields = {
                F_TITLE:    jf.get("summary", ""),
                F_JIRA_KEY: key,
                F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{key}",
                F_TYPE:     itype,
            }
            if lark_assignee: fields[F_ASSIGNEE]     = [lark_assignee]
            if sp_str:        fields[F_MD]           = sp_str
            if jira_status:   fields[F_JIRA_STATUS]  = jira_status
            if actual_start:  fields[F_ACTUAL_START] = actual_start
            if actual_end:    fields[F_ACTUAL_END]   = actual_end
            try:
                rid = lark_api.create_record(token, cfg["LARK_BASE_TOKEN"],
                                             cfg["LARK_TABLE_ID"], fields)
                dedup.mark(f"lark:{rid}")
                index.add(key, rid)
                log.info(f"Reconcile: created Lark {rid} ({key})")
            except Exception as e:
                log.error(f"Reconcile: create for {key}: {e}")

    log.info("Reconcile: done")


def backfill(cfg: dict) -> dict:
    """Bidirectional backfill for pre-existing unlinked records.

    Steps:
      1. Remove duplicate Lark records that share the same Jira Key
      2. Match unlinked Lark records to Jira issues by exact title (skip ambiguous)
      3. Create Lark records for Jira issues still without a Lark record
      4. Create Jira issues for Lark records still unlinked (allowed types only)
      5. Sync field values on all linked records (Jira → Lark)

    Returns summary dict.
    """
    log.info("backfill: starting")
    try:
        token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
        lark_records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
        jira_issues = jira_api.fetch_all_issues(cfg, types=list(config.get_allowed_jira_types()))
    except Exception as e:
        log.error(f"backfill: fetch failed — {e}")
        raise

    jira_by_key = {i["key"]: i for i in jira_issues}
    allowed_jira = config.get_allowed_jira_types()
    allowed_lark = config.get_allowed_lark_types()

    # ── Step 1: Remove duplicate Lark records — keep the one whose title ──
    #           matches the Jira issue summary; fallback to first seen      ─
    lark_grouped: dict = {}
    for rec in lark_records:
        jk = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if jk:
            lark_grouped.setdefault(jk, []).append(rec)

    lark_by_jira_key: dict = {}
    removed_duplicates = 0
    for jk, recs in lark_grouped.items():
        if len(recs) == 1:
            lark_by_jira_key[jk] = recs[0]
            continue
        jira_summary = (jira_by_key.get(jk, {}).get("fields", {}) or {}).get("summary", "") or ""
        keeper = next((r for r in recs if _lark_text(r["fields"].get(F_TITLE)) == jira_summary), recs[0])
        lark_by_jira_key[jk] = keeper
        for rec in recs:
            if rec["record_id"] == keeper["record_id"]:
                continue
            rid = rec["record_id"]
            dedup.mark(f"lark:{rid}")
            try:
                lark_api.delete_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid)
                removed_duplicates += 1
                log.warning(f"backfill: deleted duplicate Lark {rid} (kept {keeper['record_id']} for {jk})")
            except Exception as e:
                log.error(f"backfill: delete duplicate {rid}: {e}")

    # ── Step 2: Match unlinked Lark records to Jira issues by title ───────
    unlinked = [r for r in lark_records if not _lark_text(r["fields"].get(F_JIRA_KEY))]

    jira_title_counts: Counter = Counter(i["fields"].get("summary", "") for i in jira_issues)
    jira_by_title = {
        i["fields"].get("summary", ""): i["key"]
        for i in jira_issues
        if jira_title_counts[i["fields"].get("summary", "")] == 1
    }
    lark_title_counts: Counter = Counter(
        _lark_text(r["fields"].get(F_TITLE)) or "" for r in unlinked
    )

    matched, skipped_ambiguous = 0, 0
    pairs: list = []
    just_matched_ids: set = set()

    for rec in unlinked:
        title = _lark_text(rec["fields"].get(F_TITLE)) or ""
        if not title:
            continue
        if lark_title_counts[title] > 1:
            skipped_ambiguous += 1
            log.warning(f"backfill: ambiguous Lark title '{title}' — skipping")
            continue
        jira_key = jira_by_title.get(title)
        if not jira_key or jira_key in lark_by_jira_key:
            continue
        rid = rec["record_id"]
        dedup.mark(f"lark:{rid}")
        try:
            lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid, {
                F_JIRA_KEY: jira_key,
                F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{jira_key}",
            })
            index.add(jira_key, rid)
            lark_by_jira_key[jira_key] = rec
            just_matched_ids.add(rid)
            matched += 1
            pairs.append({"jira_key": jira_key, "lark_id": rid, "title": title})
            log.info(f"backfill: matched {jira_key} ↔ {rid} ('{title}')")
        except Exception as e:
            log.error(f"backfill: match {rid}: {e}")

    for title, count in jira_title_counts.items():
        if count > 1:
            skipped_ambiguous += 1

    # ── Step 3: Create Lark records for Jira issues with no Lark record ───
    created_lark = 0
    for issue in jira_issues:
        key = issue["key"]
        if key in lark_by_jira_key:
            continue
        jf = issue["fields"]
        itype = jf["issuetype"]["name"]
        if itype not in allowed_jira:
            continue
        assignee_name = (jf.get("assignee") or {}).get("displayName")
        lark_assignee = JIRA_TO_LARK_ASSIGNEE.get(assignee_name) if assignee_name else None
        sp_str = _sp_to_str(jf.get("customfield_10016"))
        jira_status = (jf.get("status") or {}).get("name")
        actual_start = _jira_datetime_to_lark_ts(jf.get("customfield_10175"))
        actual_end   = _jira_datetime_to_lark_ts(jf.get("customfield_10176"))
        parent_jira_key = (jf.get("parent") or {}).get("key")
        parent_rid = index._jira_to_lark.get(parent_jira_key) if parent_jira_key else None
        fields = {
            F_TITLE:    jf.get("summary", ""),
            F_JIRA_KEY: key,
            F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{key}",
            F_TYPE:     itype,
        }
        if lark_assignee: fields[F_ASSIGNEE]     = [lark_assignee]
        if sp_str:        fields[F_MD]           = sp_str
        if jira_status:   fields[F_JIRA_STATUS]  = jira_status
        if actual_start:  fields[F_ACTUAL_START] = actual_start
        if actual_end:    fields[F_ACTUAL_END]   = actual_end
        if parent_rid:    fields[F_PARENT]       = [parent_rid]
        try:
            rid = lark_api.create_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], fields)
            dedup.mark(f"lark:{rid}")
            index.add(key, rid)
            lark_by_jira_key[key] = {"record_id": rid, "fields": fields}
            created_lark += 1
            log.info(f"backfill: created Lark {rid} for Jira {key}")
        except Exception as e:
            log.error(f"backfill: create Lark for {key}: {e}")

    # ── Step 4: Create Jira issues for still-unlinked Lark records ────────
    created_jira = 0
    account_ids: "dict | None" = None
    for rec in unlinked:
        rid = rec["record_id"]
        if rid in just_matched_ids:
            continue
        itype = _lark_select(rec["fields"].get(F_TYPE))
        if not itype or itype not in allowed_lark:
            continue
        title = _lark_text(rec["fields"].get(F_TITLE)) or f"[Lark] {rid}"
        if account_ids is None:
            try:
                account_ids = jira_api.get_account_ids(cfg)
            except Exception:
                account_ids = {}
        assignee_lark = _lark_select(rec["fields"].get(F_ASSIGNEE))
        jira_name = LARK_TO_JIRA_ASSIGNEE.get(assignee_lark, "") if assignee_lark else ""
        assignee_id = account_ids.get(jira_name)
        start = _lark_ts_to_jira_date(rec["fields"].get(F_START))
        end   = _lark_ts_to_jira_date(rec["fields"].get(F_END))
        parent_jira_key = None
        for item in (rec["fields"].get(F_PARENT) or []):
            if isinstance(item, dict):
                p_rid = item.get("record_id") or item.get("id")
                if p_rid:
                    parent_jira_key = index._lark_to_jira.get(p_rid)
                    if parent_jira_key:
                        break
        dedup.mark(f"lark:{rid}")
        try:
            new_key = jira_api.create_issue(cfg, itype, title,
                                             start_date=start, due_date=end,
                                             assignee_id=assignee_id,
                                             parent_key=parent_jira_key)
            dedup.mark(f"jira:{new_key}")
            lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid, {
                F_JIRA_KEY: new_key,
                F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{new_key}",
            })
            index.add(new_key, rid)
            created_jira += 1
            log.info(f"backfill: created Jira {new_key} for Lark {rid}")
        except Exception as e:
            log.error(f"backfill: create Jira for {rid}: {e}")

    # ── Step 5: Sync fields on all linked records (Jira → Lark) ──────────
    synced = 0
    for jira_key, lark_rec in lark_by_jira_key.items():
        issue = jira_by_key.get(jira_key)
        if not issue:
            continue
        jf = issue["fields"]
        rid = lark_rec["record_id"]
        cur = lark_rec["fields"]
        assignee_name = (jf.get("assignee") or {}).get("displayName")
        lark_assignee = JIRA_TO_LARK_ASSIGNEE.get(assignee_name) if assignee_name else None
        sp_str = _sp_to_str(jf.get("customfield_10016"))
        jira_status  = (jf.get("status") or {}).get("name")
        actual_start = _jira_datetime_to_lark_ts(jf.get("customfield_10175"))
        actual_end   = _jira_datetime_to_lark_ts(jf.get("customfield_10176"))
        updates: dict = {}
        if lark_assignee and _lark_select(cur.get(F_ASSIGNEE)) != lark_assignee:
            updates[F_ASSIGNEE] = [lark_assignee]
        if sp_str is not None and str(cur.get(F_MD) or "") != sp_str:
            updates[F_MD] = sp_str
        if jira_status and _lark_text(cur.get(F_JIRA_STATUS)) != jira_status:
            updates[F_JIRA_STATUS] = jira_status
        if actual_start is not None and cur.get(F_ACTUAL_START) != actual_start:
            updates[F_ACTUAL_START] = actual_start
        if actual_end is not None and cur.get(F_ACTUAL_END) != actual_end:
            updates[F_ACTUAL_END] = actual_end
        if updates:
            dedup.mark(f"lark:{rid}")
            try:
                lark_api.update_record(token, cfg["LARK_BASE_TOKEN"],
                                       cfg["LARK_TABLE_ID"], rid, updates)
                synced += 1
                log.info(f"backfill: synced {jira_key} → Lark {rid}")
            except Exception as e:
                log.error(f"backfill: sync {rid}: {e}")

    result = {
        "removed_duplicates": removed_duplicates,
        "matched": matched,
        "skipped_ambiguous": skipped_ambiguous,
        "created_lark": created_lark,
        "created_jira": created_jira,
        "synced": synced,
        "pairs": pairs,
    }
    log.info(f"backfill: done — {result}")
    return result


def _sp_to_str(val) -> "str | None":
    if val is None:
        return None
    try:
        f = float(val)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return None
