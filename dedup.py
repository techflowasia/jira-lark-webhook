"""In-memory TTL dedup cache — prevents sync loops."""
import time

_cache: dict[str, float] = {}
DEDUP_TTL = 120  # outlasts Jira's 5-retry window (~10 min)


def mark(key: str) -> None:
    _cache[key] = time.time() + DEDUP_TTL


def is_ours(key: str) -> bool:
    exp = _cache.get(key, 0)
    if exp and exp > time.time():
        return True
    _cache.pop(key, None)  # lazy cleanup
    return False


def date_echo_key(jira_key: str, slot: str, jira_date: "str | None") -> str:
    """Canonical echo-suppression key for a synced date field.

    `slot` is "start" or "end"; `jira_date` is the canonical Jira "YYYY-MM-DD"
    string (what both _lark_ts_to_jira_date and Jira's own value resolve to).
    When one update handler writes a date to the other side it marks this key;
    the mirrored handler, seeing the resulting webhook, finds it `is_ours` and
    skips re-propagating — breaking a bidirectional conflict ping-pong that
    value-comparison alone can't converge under concurrent processing. A
    genuinely new edit carries a different date string, so it is NOT
    suppressed (no legitimate edit is dropped)."""
    return f"dateecho:{jira_key}:{slot}:{jira_date}"
