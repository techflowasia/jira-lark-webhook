import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import dedup


def setup_function():
    dedup._cache.clear()


def test_mark_is_ours():
    dedup.mark("jira:TEST-1")
    assert dedup.is_ours("jira:TEST-1")


def test_mark_survives_multiple_checks():
    dedup.mark("lark:rec123")
    assert dedup.is_ours("lark:rec123")
    assert dedup.is_ours("lark:rec123")  # get() not pop() — mark not consumed


def test_expired_returns_false():
    dedup._cache["jira:TEST-2"] = time.time() - 1
    assert not dedup.is_ours("jira:TEST-2")


def test_expired_cleans_up():
    dedup._cache["jira:TEST-3"] = time.time() - 1
    dedup.is_ours("jira:TEST-3")
    assert "jira:TEST-3" not in dedup._cache


def test_unknown_key_returns_false():
    assert not dedup.is_ours("jira:UNKNOWN-999")


def test_different_keys_independent():
    dedup.mark("jira:A-1")
    assert dedup.is_ours("jira:A-1")
    assert not dedup.is_ours("jira:A-2")
