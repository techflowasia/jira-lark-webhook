"""Sync event history — persists to Supabase, falls back to in-memory deque."""
import os
import logging
from collections import deque
from datetime import datetime
from config import LOCAL_TZ, TZ_LABEL

log = logging.getLogger(__name__)
_TZ7 = LOCAL_TZ
_TS_FMT = f"%Y-%m-%d %H:%M:%S {TZ_LABEL}"

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


def _current_project_id() -> "int | None":
    try:
        import projects  # lazy: projects imports history
        return projects.current_id()
    except Exception as e:
        log.warning(f"history: could not resolve current project: {e}")
        return None


def record(*, direction: str, event: str, lark_id: str = "",
           jira_key: str = "", description: str, status: str = "ok",
           error: str = "", type: str = "") -> None:
    ts_str = datetime.now(_TZ7).strftime(_TS_FMT)
    project_id = _current_project_id()
    mem = {"ts": ts_str, "direction": direction, "event": event,
           "lark_id": lark_id, "jira_key": jira_key, "type": type,
           "description": description, "status": status, "error": error,
           "project_id": project_id}
    _fallback.appendleft(mem)

    client = _get_client()
    if client:
        payload = {
            "direction": direction, "event": event,
            "lark_id": lark_id, "jira_key": jira_key, "type": type,
            "description": description, "status": status, "error": error,
            "project_id": project_id,
        }
        # A column missing from sync_history (migration 001 / 003 not applied)
        # must not lose the event: drop that column and retry.
        for _ in range(3):
            try:
                client.table("sync_history").insert(payload).execute()
                return
            except Exception as e:
                msg = str(e)
                missing = next((c for c in ("project_id", "type") if c in payload and c in msg
                                and ("column" in msg.lower() or "schema" in msg.lower())), None)
                if missing is None:
                    log.warning(f"history.record Supabase insert failed: {e}")
                    return
                hint = {"type": "migrations/001_add_type_column.sql",
                        "project_id": "migrations/003_multi_project.sql"}[missing]
                log.warning(f"history.record: '{missing}' column missing from sync_history — "
                            f"dropping field and retrying. Run {hint}")
                payload.pop(missing)


def query(from_dt: "datetime | None" = None, to_dt: "datetime | None" = None,
          jira_key: str = "", page: int = 1, page_size: int = 50,
          project_id: "int | None" = None) -> dict:
    """Return {rows, total, page, pages} from Supabase (falls back to in-memory).
    project_id filters to one project's rows when given."""
    client = _get_client()
    if client:
        try:
            q = client.table("sync_history").select("*", count="exact")
            if project_id is not None:
                q = q.eq("project_id", project_id)
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
                        r["ts"] = dt.astimezone(_TZ7).strftime(_TS_FMT)
                    except Exception:
                        pass
            pages = max(1, (total + page_size - 1) // page_size)
            return {"rows": rows, "total": total, "page": page, "pages": pages}
        except Exception as e:
            log.warning(f"history.query Supabase failed: {e}")

    return _fallback_query(from_dt, to_dt, jira_key, page, page_size, project_id)


def _fallback_query(from_dt, to_dt, jira_key, page, page_size, project_id=None):
    rows = list(_fallback)
    if project_id is not None:
        rows = [r for r in rows if r.get("project_id") == project_id]
    if jira_key:
        rows = [r for r in rows if jira_key.lower() in r.get("jira_key", "").lower()]
    if from_dt or to_dt:
        filtered = []
        for r in rows:
            try:
                ts = datetime.strptime(r["ts"], _TS_FMT).replace(tzinfo=_TZ7)
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


def error_count(project_id: int, since: datetime) -> int:
    """Number of status='error' history rows of one project since `since`."""
    client = _get_client()
    if client:
        try:
            res = (client.table("sync_history").select("id", count="exact")
                   .eq("project_id", project_id).eq("status", "error")
                   .gte("ts", since.isoformat()).execute())
            return res.count or 0
        except Exception as e:
            log.warning(f"history.error_count Supabase failed: {e}")
    rows = _fallback_query(since, None, "", 1, max(1, len(_fallback)), project_id)["rows"]
    return sum(1 for r in rows if r.get("status") == "error")


def recent(n: int = 200) -> list:
    return list(_fallback)[:n]


def warn_unmapped_assignee(*, jira_key: str, name: str, outcome: str,
                           lark_id: str = "", direction: str = "lark→jira",
                           type: str = "") -> None:
    """One skipped history row for a Lark assignee who is not in the
    project's people list (Lark->Jira; Jira is left unchanged). Jira->Lark
    never warns: an unmapped Jira assignee is auto-added instead (see
    config.lark_assignee_for). Callers rate-limit / call it once per event."""
    record(direction=direction, event="updated", jira_key=jira_key, lark_id=lark_id,
           description=f"Assignee '{name}' not in people list — {outcome}",
           status="skipped", type=type)
