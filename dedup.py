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
