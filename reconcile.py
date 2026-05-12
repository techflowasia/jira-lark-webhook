"""30-min safety-net cron: full diff Jira <-> Lark."""
import logging
from collections import Counter
import lark_api, jira_api, index, dedup, config
from config import (F_TITLE, F_JIRA_KEY, F_JIRA_URL, F_TYPE, F_ASSIGNEE,
                    F_MD, F_JIRA_STATUS, F_ACTUAL_START, F_ACTUAL_END,
                    JIRA_TO_LARK_ASSIGNEE)
from utils import _lark_text, _lark_select, _jira_datetime_to_lark_ts

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

    lark_by_jira_key = {}
    for rec in lark_records:
        jk = _lark_text(rec["fields"].get(F_JIRA_KEY))
        if jk:
            lark_by_jira_key[jk] = rec

    jira_keys = {i["key"] for i in jira_issues}
    jira_by_key = {i["key"]: i for i in jira_issues}

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


def match_by_title(cfg: dict) -> dict:
    """Link unlinked Lark records to Jira issues by exact title match.

    Skips ambiguous cases (duplicate titles on either side).
    Returns {matched, skipped_ambiguous, pairs}.
    """
    log.info("match_by_title: starting")
    try:
        token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
        lark_records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
        jira_issues = jira_api.fetch_all_issues(cfg, types=list(config.get_allowed_jira_types()))
    except Exception as e:
        log.error(f"match_by_title: fetch failed — {e}")
        raise

    # Build Jira title → key, skipping duplicates
    jira_title_counts: Counter = Counter(i["fields"].get("summary", "") for i in jira_issues)
    jira_by_title = {
        i["fields"].get("summary", ""): i["key"]
        for i in jira_issues
        if jira_title_counts[i["fields"].get("summary", "")] == 1
    }

    # Unlinked Lark records only
    unlinked = [
        rec for rec in lark_records
        if not _lark_text(rec["fields"].get(F_JIRA_KEY))
    ]

    # Skip Lark-side title duplicates
    lark_title_counts: Counter = Counter(
        _lark_text(rec["fields"].get(F_TITLE)) or "" for rec in unlinked
    )

    pairs, skipped = [], 0
    for rec in unlinked:
        title = _lark_text(rec["fields"].get(F_TITLE)) or ""
        if not title:
            continue
        if lark_title_counts[title] > 1:
            log.warning(f"match_by_title: ambiguous Lark title '{title}' — skipping")
            skipped += 1
            continue
        jira_key = jira_by_title.get(title)
        if not jira_key:
            continue
        rid = rec["record_id"]
        dedup.mark(f"lark:{rid}")
        try:
            lark_api.update_record(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"], rid, {
                F_JIRA_KEY: jira_key,
                F_JIRA_URL: f"https://{cfg['JIRA_DOMAIN']}/browse/{jira_key}",
            })
            index.add(jira_key, rid)
            pairs.append({"jira_key": jira_key, "lark_id": rid, "title": title})
            log.info(f"match_by_title: linked {jira_key} ↔ {rid} ('{title}')")
        except Exception as e:
            log.error(f"match_by_title: update {rid}: {e}")

    # Log skipped ambiguous Jira titles too
    for title, count in jira_title_counts.items():
        if count > 1:
            log.warning(f"match_by_title: ambiguous Jira title '{title}' ({count} issues) — skipping")
            skipped += 1

    log.info(f"match_by_title: done — matched={len(pairs)} skipped_ambiguous={skipped}")
    return {"matched": len(pairs), "skipped_ambiguous": skipped, "pairs": pairs}


def _sp_to_str(val) -> "str | None":
    if val is None:
        return None
    try:
        f = float(val)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return None
