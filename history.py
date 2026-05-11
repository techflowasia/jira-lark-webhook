"""In-memory ring buffer for sync event history."""
from collections import deque
from datetime import datetime, timezone

_log: deque = deque(maxlen=500)


def record(*, direction: str, event: str, lark_id: str = "",
           jira_key: str = "", description: str, status: str = "ok",
           error: str = "") -> None:
    _log.appendleft({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "direction": direction,
        "event": event,
        "lark_id": lark_id,
        "jira_key": jira_key,
        "description": description,
        "status": status,
        "error": error,
    })


def recent(n: int = 200) -> list:
    return list(_log)[:n]
