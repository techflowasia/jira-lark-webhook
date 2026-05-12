"""Tests for utils helpers."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils import _lark_link_rid


def test_lark_link_rid_v1_record_ids_plural():
    """Current Lark v1 Bitable shape — record_ids is a list."""
    value = [{"record_ids": ["rec1"], "text": "x", "type": "text"}]
    assert _lark_link_rid(value) == "rec1"


def test_lark_link_rid_legacy_record_id_singular():
    assert _lark_link_rid([{"record_id": "rec1"}]) == "rec1"


def test_lark_link_rid_legacy_id():
    assert _lark_link_rid([{"id": "rec1"}]) == "rec1"


def test_lark_link_rid_empty_list():
    assert _lark_link_rid([]) is None


def test_lark_link_rid_none():
    assert _lark_link_rid(None) is None


def test_lark_link_rid_multiple_links_returns_first():
    value = [
        {"record_ids": ["recA"], "text": "A"},
        {"record_ids": ["recB"], "text": "B"},
    ]
    assert _lark_link_rid(value) == "recA"


def test_lark_link_rid_skips_non_dict_items():
    value = ["junk", None, {"record_ids": ["recC"]}]
    assert _lark_link_rid(value) == "recC"


def test_lark_link_rid_accepts_single_dict_not_in_list():
    assert _lark_link_rid({"record_ids": ["rec1"]}) == "rec1"
