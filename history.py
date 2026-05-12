"""Sync event history — persists to Supabase, falls back to in-memory deque."""
import os
import logging
from collections import deque
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)
_TZ7 = timezone(timedelta(hours=7))

_fallback: deque = deque(maxlen=500)
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception as e:
        log.warning(f"Supabase init failed: {e}")
        return None


def record(*, direction: str, event: str, lark_id: str = "",
           jira_key: str = "", description: str, status: str = "ok",
           error: str = "", type: str = "") -> None:
    ts_str = datetime.now(_TZ7).strftime("%Y-%m-%d %H:%M:%S +07")
    mem = {"ts": ts_str, "direction": direction, "event": event,
           "lark_id": lark_id, "jira_key": jira_key, "type": type,
           "description": description, "status": status, "error": error}
    _fallback.appendleft(mem)

    client = _get_client()
    if client:
        payload = {
            "direction": direction, "event": event,
            "lark_id": lark_id, "jira_key": jira_key, "type": type,
            "description": description, "status": status, "error": error,
        }
        try:
            client.table("sync_history").insert(payload).execute()
        except Exception as e:
            msg = str(e)
            if "type" in msg and ("column" in msg.lower() or "schema" in msg.lower()):
                log.warning(
                    "history.record: 'type' column missing from sync_history — "
                    "dropping field and retrying. Run: "
                    "ALTER TABLE sync_history ADD COLUMN type text; "
                    "(see migrations/001_add_type_column.sql)"
                )
                payload.pop("type", None)
                try:
                    client.table("sync_history").insert(payload).execute()
                    return
                except Exception as e2:
                    log.warning(f"history.record Supabase insert (retry) failed: {e2}")
                    return
            log.warning(f"history.record Supabase insert failed: {e}")


def query(from_dt: "datetime | None" = None, to_dt: "datetime | None" = None,
          jira_key: str = "", page: int = 1, page_size: int = 50) -> dict:
    """Return {rows, total, page, pages} from Supabase (falls back to in-memory)."""
    client = _get_client()
    if client:
        try:
            q = client.table("sync_history").select("*", count="exact")
            if from_dt:
                q = q.gte("ts", from_dt.isoformat())
            if to_dt:
                q = q.lte("ts", to_dt.isoformat())
            if jira_key:
                q = q.ilike("jira_key", f"%{jira_key}%")
            q = q.order("ts", desc=True)
            offset = (page - 1) * page_size
            q = q.range(offset, offset + page_size - 1)
            res = q.execute()
            total = res.count or 0
            rows = res.data or []
            for r in rows:
                if r.get("ts"):
                    try:
                        dt = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                        r["ts"] = dt.astimezone(_TZ7).strftime("%Y-%m-%d %H:%M:%S +07")
                    except Exception:
                        pass
            pages = max(1, (total + page_size - 1) // page_size)
            return {"rows": rows, "total": total, "page": page, "pages": pages}
        except Exception as e:
            log.warning(f"history.query Supabase failed: {e}")

    return _fallback_query(from_dt, to_dt, jira_key, page, page_size)


def _fallback_query(from_dt, to_dt, jira_key, page, page_size):
    rows = list(_fallback)
    if jira_key:
        rows = [r for r in rows if jira_key.lower() in r.get("jira_key", "").lower()]
    if from_dt or to_dt:
        filtered = []
        for r in rows:
            try:
                ts = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S +07").replace(tzinfo=_TZ7)
                if from_dt and ts < from_dt:
                    continue
                if to_dt and ts > to_dt:
                    continue
            except Exception:
                pass
            filtered.append(r)
        rows = filtered
    total = len(rows)
    offset = (page - 1) * page_size
    pages = max(1, (total + page_size - 1) // page_size)
    return {"rows": rows[offset:offset + page_size], "total": total, "page": page, "pages": pages}


def recent(n: int = 200) -> list:
    return list(_fallback)[:n]
