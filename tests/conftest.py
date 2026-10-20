"""Shared fixtures. `fake_supabase` is an in-memory stand-in for the chained
supabase-py calls the code base uses; later tasks reuse it."""
import pytest


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload = "select", None
        self.filters, self._order, self._range = [], None, None

    # operations
    def select(self, cols="*", count=None):
        self.op = "select"
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload = "upsert", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    # filters
    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def is_(self, col, val):
        want_null = str(val).lower() == "null"
        self.filters.append(lambda r: (r.get(col) is None) == want_null)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) >= val)
        return self

    def lte(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) <= val)
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def limit(self, n):
        self._range = (0, n - 1)
        return self

    def _match(self):
        return [r for r in self.db.tables.setdefault(self.name, []) if all(f(r) for f in self.filters)]

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        if self.op == "select":
            out = self._match()
            if self._order:
                col, desc = self._order
                out = sorted(out, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._range:
                out = out[self._range[0]:self._range[1] + 1]
            return _Result([dict(r) for r in out], count=len(out))
        if self.op == "insert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = [self.db.insert_row(self.name, dict(i)) for i in items]
            return _Result([dict(r) for r in out])
        if self.op == "update":
            hit = self._match()
            for r in hit:
                r.update(self.payload)
            return _Result([dict(r) for r in hit])
        if self.op == "upsert":
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for i in items:
                pk = "key" if self.name == "settings" else "id"
                existing = next((r for r in rows if pk in i and r.get(pk) == i[pk]), None)
                if existing:
                    existing.update(i)
                    out.append(existing)
                else:
                    out.append(self.db.insert_row(self.name, dict(i)))
            return _Result([dict(r) for r in out])
        if self.op == "delete":
            hit = self._match()
            rows[:] = [r for r in rows if r not in hit]
            return _Result([dict(r) for r in hit])
        raise AssertionError(self.op)


class FakeSupabase:
    UNIQUE = {"sync_projects": [("jira_project_key",), ("lark_base_token", "lark_table_id")]}

    def __init__(self):
        self.tables = {}
        self._seq = {}

    def table(self, name):
        return _Query(self, name)

    def insert_row(self, name, row):
        rows = self.tables.setdefault(name, [])
        for cols in self.UNIQUE.get(name, []):
            if any(all(r.get(c) == row.get(c) for c in cols) for r in rows):
                raise Exception(f"duplicate key value violates unique constraint {cols}")
        if name != "settings" and row.get("id") is None:
            self._seq[name] = self._seq.get(name, 0) + 1
            row["id"] = self._seq[name]
        elif row.get("id") is not None:
            self._seq[name] = max(self._seq.get(name, 0), row["id"])
        rows.append(row)
        return row

    # test helpers
    def rows(self, name):
        return [dict(r) for r in self.tables.get(name, [])]

    def count(self, name):
        return len(self.tables.get(name, []))

    def set_setting(self, key, value):
        self.insert_row("settings", {"key": key, "value": value})


@pytest.fixture
def fake_supabase(monkeypatch):
    import history
    fake = FakeSupabase()
    monkeypatch.setattr(history, "_get_client", lambda: fake)
    return fake


P2_BASE, P2_TABLE = "base2", "tbl2"
P2_ASSIGNEES = {"Ada Lovelace": "Ada"}
P2_TYPES = ["Epic", "Bug"]


@pytest.fixture
def two_projects():
    """Registry with the env project #1 plus a project #2 (`base2`/`tbl2`,
    Lark Title column named "Name", its own people and sync types).
    Works alone (in-memory registry) or after `fake_supabase`. Restores the
    registry and every per-project cache it touched afterwards."""
    import field_mappings, index, lark_handler, projects
    saved = (projects._projects, projects._persisted, dict(field_mappings._by_project))
    projects._projects, projects._persisted = {}, False
    projects.load_all()
    p2 = projects.create({"name": "Two", "jira_project_key": "TWO",
                          "lark_base_token": P2_BASE, "lark_table_id": P2_TABLE})
    p2 = projects.update(p2.id, {"assignee_map": dict(P2_ASSIGNEES),
                                 "allowed_jira_types": list(P2_TYPES),
                                 "allowed_lark_types": list(P2_TYPES),
                                 "sync_enabled": True, "setup_state": "active"})
    field_mappings._by_project[p2.id] = [
        dict(d, lark_field="Name") if d["jira_field"] == "summary" else dict(d)
        for d in field_mappings._DEFAULTS]
    yield projects.get(min(projects._projects)), p2
    for cache in (index._maps, lark_handler._account_ids_cache,
                  lark_handler._version_cache, lark_handler._sprint_cache):
        cache.pop(p2.id, None)
    projects._projects, projects._persisted, fm = saved
    field_mappings._by_project.clear()
    field_mappings._by_project.update(fm)
