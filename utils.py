"""Field parse helpers — copied from jira-lark-sync/sync_engine.py."""
import re
from datetime import datetime, timezone

# Lark Bitable Date fields store/return the value as UTC midnight of the
# calendar day (time-of-day is truncated to 00:00:00Z). Date-only conversions
# MUST use UTC, not a local offset: a Bangkok-midnight instant is 17:00Z the
# *previous* day, which Lark truncates to the previous calendar day — every
# Jira↔Lark round-trip then loses a day and the value-compare loop guard never
# converges (the 2026-05 runaway rewrite incident). _jira_datetime_to_lark_ts
# below handles real datetimes (with their own offset) and is unaffected.


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _lark_text(field_val) -> "str | None":
    if field_val is None:
        return None
    if isinstance(field_val, str):
        return field_val or None
    if isinstance(field_val, list):
        parts = []
        for item in field_val:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts) or None
    return str(field_val) or None


def _lark_link_rid(value) -> "str | None":
    """First linked record_id from a Lark link/two-way-link field.

    Lark v1 Bitable returns link fields as:
      [{"record_ids": ["rec..."], "text": "...", ...}, ...]
    Legacy/edge shapes tolerated: {"record_id": "rec..."} or {"id": "rec..."}.
    """
    if not value:
        return None
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, dict):
            continue
        rids = item.get("record_ids")
        if isinstance(rids, list) and rids:
            return rids[0]
        rid = item.get("record_id") or item.get("id")
        if rid:
            return rid
    return None


def _lark_select(field_val) -> "str | None":
    if field_val is None:
        return None
    if isinstance(field_val, str):
        return field_val or None
    if isinstance(field_val, dict):
        return field_val.get("text") or field_val.get("name")
    if isinstance(field_val, list) and field_val:
        item = field_val[0]
        return item.get("text") or item.get("name") if isinstance(item, dict) else str(item)
    return None


def _lark_multi(field_val) -> list:
    """All option names from a Lark multi-select value, order-preserving.

    get_record returns multi-select as ["A", "B"] (list of names) or a list
    of {"text"/"name": ...} dicts. A bare string is treated as one value.
    Used for set-based comparison so a multi-value Release doesn't trigger
    a redundant write (and a sync loop) on every reconcile/changelog pass.
    """
    if field_val is None:
        return []
    if isinstance(field_val, str):
        return [field_val] if field_val else []
    if isinstance(field_val, dict):
        v = field_val.get("text") or field_val.get("name")
        return [v] if v else []
    if isinstance(field_val, list):
        out = []
        for item in field_val:
            if isinstance(item, dict):
                v = item.get("text") or item.get("name")
            else:
                v = item
            if v:
                out.append(v)
        return out
    return []


def _jira_date_to_lark_ts(date_str: "str | None") -> "int | None":
    """Jira date ("YYYY-MM-DD") → Lark ms timestamp at UTC midnight.

    UTC (not Bangkok) so it round-trips exactly with _lark_ts_to_jira_date and
    matches what Lark Bitable stores for a Date field (UTC-midnight of the
    day). Bangkok midnight here is 17:00Z the previous day, which Lark
    truncates down a day → permanent one-day drift + a non-converging
    value-compare loop. See the module header.
    """
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _jira_datetime_to_lark_ts(dt_str: "str | None") -> "int | None":
    if not dt_str:
        return None
    try:
        normalized = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', dt_str)
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _lark_ts_to_jira_date(ts_ms) -> "str | None":
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None
