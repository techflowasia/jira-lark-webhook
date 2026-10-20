"""Server-rendered dashboard (layout C): project sidebar + per-project page +
all-activity audit. Every user-controlled string is passed through
`html.escape` before it reaches the page; the inline JS escapes with `esc()`.
"""
import html as _html
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

import config as _cfg_mod
import history
import index
import lark_api
import projects

HEALTH_COLORS = {"ok": "#22c55e", "error": "#ef4444", "paused": "#f59e0b", "draft": "#94a3b8"}
_RANGE_DAYS = {"1d": 1, "3d": 3, "7d": 7, "1m": 30}


def e(v) -> str:
    return _html.escape("" if v is None else str(v), quote=True)


# ── history filters ─────────────────────────────────────────────────────────

def parse_filters(request) -> dict:
    qp = request.query_params
    range_param = qp.get("range", "1d")
    from_s, to_s = qp.get("from_date", ""), qp.get("to_date", "")
    q = qp.get("q", "").strip()
    try:
        page = max(1, int(qp.get("page", "1") or "1"))
    except ValueError:
        page = 1
    now = datetime.now(timezone.utc)
    if range_param == "custom":
        def _d(s):
            try:
                return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None
            except ValueError:
                return None
        from_dt, to_dt = _d(from_s), _d(to_s)
    else:
        if range_param not in _RANGE_DAYS:
            range_param = "1d"
        from_dt, to_dt = now - timedelta(days=_RANGE_DAYS[range_param]), now
    return {"range": range_param, "from_date": from_s, "to_date": to_s, "q": q,
            "page": page, "from_dt": from_dt, "to_dt": to_dt, "now": now}


def fetch_history(request, project_id: "int | None") -> dict:
    f = parse_filters(request)
    return history.query(from_dt=f["from_dt"], to_dt=f["to_dt"], jira_key=f["q"],
                         page=f["page"], project_id=project_id)


# ── shared pieces ───────────────────────────────────────────────────────────

_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { overflow-x: hidden; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f1f5f9; color: #1e293b; }
  .layout { display: flex; min-height: 100vh; }
  .sidebar { width: 250px; flex: none; background: #0f172a; color: #e2e8f0;
             display: flex; flex-direction: column; position: sticky; top: 0;
             height: 100vh; overflow-y: auto; }
  .sb-title { padding: 18px 18px 12px; font-size: 15px; font-weight: 600; color: #f8fafc; }
  .sb-list { display: flex; flex-direction: column; padding: 0 8px; }
  .sb-item { display: flex; align-items: center; gap: 9px; padding: 9px 10px;
             border-radius: 6px; color: #cbd5e1; text-decoration: none; font-size: 13px; }
  .sb-item:hover { background: #1e293b; }
  .sb-item.sel { background: #1e293b; color: #fff; font-weight: 600; }
  .sb-item .hd { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .sb-item .pn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sb-item .pk { color: #64748b; font-size: 11px; margin-left: auto; font-family: monospace; }
  .sb-sep { height: 1px; background: #1e293b; margin: 10px 18px; }
  .sb-new { color: #93c5fd; }
  .sb-foot { margin-top: auto; padding: 14px 18px; border-top: 1px solid #1e293b;
             font-size: 12px; display: flex; flex-direction: column; gap: 8px; }
  .sb-foot a { color: #94a3b8; text-decoration: none; margin-right: 10px; }
  .sb-foot a:hover { color: #e2e8f0; }
  .sb-cache { color: #fff; border: none; border-radius: 6px; padding: 6px 10px;
              font-size: 12px; font-weight: 600; cursor: pointer; width: 100%; }
  .menu-bar { display: none; background: #0f172a; color: #f8fafc; padding: 10px 16px;
              align-items: center; gap: 12px; }
  .menu-btn { background: #1e293b; color: #f8fafc; border: none; border-radius: 6px;
              padding: 6px 12px; font-size: 16px; cursor: pointer; }
  .content { flex: 1; min-width: 0; }
  .header { background: #fff; padding: 18px 32px; border-bottom: 1px solid #e2e8f0;
            display: flex; align-items: center; justify-content: space-between;
            gap: 12px; flex-wrap: wrap; }
  .header-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .header h1 { font-size: 20px; font-weight: 600; overflow-wrap: anywhere; }
  .header .key { color: #64748b; font-family: monospace; font-size: 13px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .toggle-btn { color: #fff; border: none; cursor: pointer; padding: 8px 18px;
                border-radius: 6px; font-size: 13px; font-weight: 600; display: inline-block; }
  .toggle-btn:hover { opacity: 0.88; }
  .main { padding: 24px 32px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr));
           gap: 16px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 10px; padding: 18px 20px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .card .label { font-size: 11px; font-weight: 600; text-transform: uppercase;
                 letter-spacing: .05em; color: #64748b; margin-bottom: 4px; }
  .card .value { font-size: 22px; font-weight: 700; color: #0f172a; }
  .card .sub { font-size: 12px; color: #64748b; margin-top: 2px; word-break: break-all; }
  .section { background: #fff; border-radius: 10px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; margin-bottom: 24px; }
  .section-header { padding: 14px 20px; border-bottom: 1px solid #e2e8f0;
                    font-weight: 600; font-size: 14px; background: #f8fafc; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { padding: 10px 14px; text-align: left; font-size: 11px; font-weight: 600;
       text-transform: uppercase; letter-spacing: .04em; color: #64748b;
       border-bottom: 1px solid #e2e8f0; background: #f8fafc; }
  td { padding: 10px 14px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr.error td { background: #fff5f5; }
  tr.skip  td { background: #fafaf0; color: #888; }
  tr.sys   td { background: #faf5ff; color: #7c3aed; }
  .ts { color: #64748b; font-size: 11px; white-space: nowrap; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 11px; font-weight: 600; white-space: nowrap; }
  .badge.lark { background: #eff6ff; color: #1d4ed8; }
  .badge.jira { background: #f0fdf4; color: #15803d; }
  .badge.sys  { background: #faf5ff; color: #7c3aed; }
  .evbadge { display: inline-block; padding: 2px 8px; border-radius: 4px;
             font-size: 11px; font-weight: 600; color: #fff; }
  code { background: #f1f5f9; padding: 1px 5px; border-radius: 3px;
         font-size: 12px; font-family: monospace; }
  code.small { font-size: 10px; }
  .errmsg { color: #dc2626; font-size: 11px; margin-top: 3px; font-family: monospace; }
  .empty { text-align: center; color: #94a3b8; padding: 40px !important; }
  .cfg-key { color: #64748b; font-size: 12px; width: 160px; }
  .cfg-val { font-family: monospace; font-size: 12px; overflow-wrap: anywhere; }
  .paused-banner { background: #fef3c7; border: 1px solid #f59e0b; border-radius: 8px;
                   padding: 12px 18px; margin-bottom: 20px; color: #92400e;
                   font-size: 13px; font-weight: 500; }
  .draft-banner { background: #f1f5f9; border: 1px solid #94a3b8; border-radius: 8px;
                  padding: 12px 18px; margin-bottom: 20px; color: #334155;
                  font-size: 13px; font-weight: 500; }
  .draft-banner a { color: #1d4ed8; font-weight: 600; }
  .filter-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
                padding: 12px 20px; border-bottom: 1px solid #e2e8f0; background: #f8fafc; }
  .rbtn { padding: 5px 14px; border-radius: 20px; font-size: 12px; font-weight: 600;
          text-decoration: none; color: #64748b; background: #e2e8f0; border: none; cursor: pointer; }
  .rbtn:hover { background: #cbd5e1; }
  .rbtn.active { background: #0f172a; color: #fff; }
  .custom-range { align-items: center; gap: 6px; font-size: 12px; color: #64748b; flex-wrap: wrap; }
  .custom-range input[type=date] { padding: 4px 8px; border: 1px solid #cbd5e1;
    border-radius: 6px; font-size: 12px; color: #1e293b; }
  .custom-range button, .search-box button { padding: 5px 12px; background: #0f172a; color: #fff;
    border: none; border-radius: 6px; font-size: 12px; cursor: pointer; }
  .search-box { margin-left: auto; display: flex; gap: 6px; }
  .search-box input { padding: 5px 10px; border: 1px solid #cbd5e1; border-radius: 6px;
    font-size: 12px; width: 180px; }
  .proj-filter select { padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 12px; }
  .pagination { display: flex; align-items: center; justify-content: center; gap: 12px;
                padding: 16px 20px; border-top: 1px solid #e2e8f0; }
  .pg-btn { padding: 6px 16px; border-radius: 6px; font-size: 13px; font-weight: 600;
            text-decoration: none; background: #0f172a; color: #fff; }
  .pg-btn.disabled { background: #e2e8f0; color: #94a3b8; cursor: not-allowed; pointer-events: none; }
  .pg-info { font-size: 13px; color: #64748b; }
  .change-btn { margin-left: 10px; padding: 3px 10px; font-size: 11px; font-weight: 600;
                background: #e2e8f0; border: none; border-radius: 4px; cursor: pointer; color: #1e293b; }
  .change-btn:hover { background: #cbd5e1; }
  #field-table input, #field-table select, #people-table input { padding: 3px 6px;
    border: 1px solid #cbd5e1; border-radius: 4px; font-size: 12px; width: 100%; box-sizing: border-box; }
  .fm-field-sel { min-width: 160px; max-width: 240px; }
  .fm-label-inp { max-width: 120px; }
  .fm-edit-btn { padding: 3px 8px; font-size: 11px; border: none; border-radius: 4px;
    cursor: pointer; margin-right: 3px; }
  .fm-save { background: #22c55e; color: #fff; }
  .fm-cancel { background: #e2e8f0; color: #1e293b; }
  .fm-delete { background: #ef4444; color: #fff; }
  .fm-edit { background: #3b82f6; color: #fff; }
  tr.new-row td { background: #f0fdf4; }
  .table-list { display: flex; flex-direction: column; gap: 6px; max-width: 420px; }
  .table-item { display: flex; align-items: center; justify-content: space-between;
                padding: 8px 12px; border: 1px solid #e2e8f0; border-radius: 6px;
                background: #f8fafc; font-size: 13px; gap: 8px; }
  .table-item.active-tbl { border-color: #0f172a; background: #f0f9ff; font-weight: 600; }
  .table-item button { padding: 3px 10px; font-size: 11px; font-weight: 600; border: none;
                       border-radius: 4px; cursor: pointer; background: #0f172a; color: #fff; }
  .table-item button:disabled { background: #94a3b8; cursor: not-allowed; }
  .tbl-id { font-size: 10px; color: #94a3b8; font-family: monospace; }
  .sync-types-panels { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; padding: 16px 20px; }
  .sync-panel { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; }
  .sync-panel h3 { font-size: 13px; font-weight: 600; color: #0f172a; margin-bottom: 10px; }
  .sync-panel label { display: flex; align-items: center; gap: 8px; font-size: 13px;
                      padding: 4px 0; cursor: pointer; color: #1e293b; }
  .sync-panel label input[type=checkbox] { width: 15px; height: 15px; cursor: pointer; }
  .sync-save-row { padding: 12px 20px; border-top: 1px solid #e2e8f0; display: flex;
                   align-items: center; gap: 12px; }
  .sync-save-btn { padding: 6px 18px; background: #0f172a; color: #fff; border: none;
                   border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .sync-save-btn:hover { opacity: 0.88; }
  .sync-msg { font-size: 12px; color: #22c55e; }
  .unmapped-badge { display: inline-block; margin-left: 10px; padding: 2px 9px; border-radius: 10px;
                    font-size: 11px; font-weight: 600; background: #fef3c7; color: #92400e; }
  .unmapped-badge.ok { background: #dcfce7; color: #166534; }

  @media (max-width: 767px) {
    .layout { flex-direction: column; }
    .menu-bar { display: flex; }
    .sidebar { display: none; width: 100%; height: auto; position: static; }
    .sidebar.open { display: flex; }
    .header { padding: 12px 16px; }
    .header h1 { font-size: 16px; }
    .toggle-btn { padding: 6px 12px; font-size: 12px; }
    .main { padding: 12px 16px; }
    .section { overflow-x: auto; border-radius: 8px; }
    table { font-size: 12px; min-width: 480px; }
    th, td { padding: 8px 10px; }
    #history-table .col-lark { display: none; }
    #field-table th:nth-child(3), #field-table td:nth-child(3),
    #field-table th:nth-child(5), #field-table td:nth-child(5) { display: none; }
    .filter-bar { gap: 6px; padding: 10px 12px; }
    .search-box { margin-left: 0; width: 100%; }
    .search-box input { flex: 1; width: auto; min-width: 0; }
    .sync-types-panels { grid-template-columns: 1fr; gap: 12px; padding: 12px; }
    .sync-save-row { flex-wrap: wrap; padding: 10px 12px; }
    .cfg-key { width: 110px; font-size: 11px; }
    .cfg-val { font-size: 11px; }
    .cards { gap: 10px; margin-bottom: 16px; }
    .card { padding: 14px 16px; }
    .card .value { font-size: 20px; }
    .pagination { gap: 8px; padding: 12px; }
    .pg-info { font-size: 12px; }
  }
"""


def sidebar(projects_with_health: list, selected: "int | None", activity: bool = False) -> str:
    items = ""
    for p in projects_with_health:
        color = HEALTH_COLORS.get(p.get("health"), HEALTH_COLORS["draft"])
        label = p.get("name") or p.get("jira_project_key") or f"Project {p.get('id')}"
        sel = " sel" if p.get("id") == selected else ""
        items += (f'<a class="sb-item{sel}" href="/p/{int(p["id"])}" title="{e(p.get("health"))}">'
                  f'<span class="hd" style="background:{color}"></span>'
                  f'<span class="pn">{e(label)}</span>'
                  f'<span class="pk">{e(p.get("jira_project_key"))}</span></a>')
    cache_on = lark_api._value_cache_enabled
    cache_label = "Disable Lark Cache" if cache_on else "Enable Lark Cache"
    cache_color = "#b45309" if cache_on else "#16a34a"
    act = " sel" if activity else ""
    return f"""<nav class="sidebar" id="sidebar">
  <div class="sb-title">Jira ↔ Lark Sync</div>
  <div class="sb-list">
    {items}
    <a class="sb-item sb-new" href="/p/new">+ New project</a>
    <div class="sb-sep" style="margin:6px 0"></div>
    <a class="sb-item{act}" href="/activity">All activity</a>
  </div>
  <div class="sb-foot">
    <form method="post" action="/toggle/lark-value-cache">
      <button class="sb-cache" style="background:{cache_color}" type="submit">{cache_label}</button>
    </form>
    <div><a href="/debug/payloads">payloads</a><a href="/debug/index">index</a><a href="/debug/lark-calls">lark calls</a><a href="/health">health</a></div>
  </div>
</nav>"""


def _shell(title: str, side: str, body: str, script: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="menu-bar"><button class="menu-btn" type="button" aria-label="Menu"
  onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
  <strong>Jira ↔ Lark Sync</strong></div>
<div class="layout">
{side}
<div class="content">
{body}
</div>
</div>
<script>
function toggleCustom(ev) {{
  ev.preventDefault();
  var f = document.getElementById('custom-form');
  if (f) f.style.display = f.style.display === 'none' ? 'flex' : 'none';
}}
</script>
{script}
</body>
</html>"""


# ── history table ───────────────────────────────────────────────────────────

def _row_class(entry) -> str:
    if entry.get("direction") == "system":
        return "sys"
    s = entry.get("status")
    return "error" if s == "error" else ("skip" if s == "skipped" else "")


def _direction_badge(d) -> str:
    d = d or ""
    if d == "system":
        return '<span class="badge sys">System</span>'
    if "lark" in d.split("→")[0]:
        return '<span class="badge lark">Lark → Jira</span>'
    return '<span class="badge jira">Jira → Lark</span>'


def _event_badge(ev) -> str:
    colors = {"created": "#22c55e", "updated": "#3b82f6", "deleted": "#ef4444", "config": "#a855f7"}
    return f'<span class="evbadge" style="background:{colors.get(ev, "#888")}">{e(ev)}</span>'


def _history_section(request, result: dict, base_path: str, with_project: bool,
                     project_filter: "int | None" = None) -> str:
    f = parse_filters(request)
    page, pages, total = f["page"], result["pages"], result["total"]
    names = {p.id: (p.name or p.jira_project_key) for p in projects.all_projects()}
    rows = ""
    for en in result["rows"]:
        err = f'<div class="errmsg">{e(en["error"])}</div>' if en.get("error") else ""
        proj = ""
        if with_project:
            pid = en.get("project_id")
            proj = f"<td>{e(names.get(pid, '—' if pid is None else f'#{pid}'))}</td>"
        rows += f"""
        <tr class="{_row_class(en)}">
          <td class="ts">{e(en.get("ts"))}</td>
          {proj}
          <td>{_direction_badge(en.get("direction"))}</td>
          <td>{_event_badge(en.get("event"))}</td>
          <td>{e(en.get("type") or "—")}</td>
          <td><code>{e(en.get("jira_key") or "—")}</code></td>
          <td class="col-lark"><code class="small">{e(en.get("lark_id") or "—")}</code></td>
          <td>{e(en.get("description"))}{err}</td>
        </tr>"""
    ncols = 8 if with_project else 7
    if not rows:
        rows = f'<tr><td colspan="{ncols}" class="empty">No events in this range.</td></tr>'

    extra = {"project": str(project_filter)} if (with_project and project_filter is not None) else {}

    def url(params: dict) -> str:
        merged = {**extra, **params}
        return base_path + ("?" + urllib.parse.urlencode(merged) if merged else "")

    def page_url(pg: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(pg)
        return base_path + "?" + urllib.parse.urlencode(params)

    prev_btn = (f'<a class="pg-btn" href="{e(page_url(page - 1))}">← Prev</a>'
                if page > 1 else '<span class="pg-btn disabled">← Prev</span>')
    next_btn = (f'<a class="pg-btn" href="{e(page_url(page + 1))}">Next →</a>'
                if page < pages else '<span class="pg-btn disabled">Next →</span>')

    def rbtn(r, label):
        params = {k: v for k, v in {"range": r, "q": f["q"]}.items() if v}
        active = "active" if f["range"] == r else ""
        return f'<a class="rbtn {active}" href="{e(url(params))}">{label}</a>'

    now = f["now"]
    custom_style = "display:flex" if f["range"] == "custom" else "display:none"
    custom_from = f["from_date"] or (now - timedelta(days=7)).strftime("%Y-%m-%d")
    custom_to = f["to_date"] or now.strftime("%Y-%m-%d")
    hidden_proj = (f'<input type="hidden" name="project" value="{e(project_filter)}">'
                   if extra else "")
    from_hidden = f'<input type="hidden" name="from_date" value="{e(f["from_date"])}">' if f["from_date"] else ""
    to_hidden = f'<input type="hidden" name="to_date" value="{e(f["to_date"])}">' if f["to_date"] else ""
    proj_select = ""
    if with_project:
        opts = '<option value="">All projects</option>' + "".join(
            f'<option value="{p.id}"{" selected" if p.id == project_filter else ""}>'
            f'{e(p.name or p.jira_project_key)}</option>' for p in projects.all_projects())
        proj_select = (f'<form class="proj-filter" method="get" action="{base_path}">'
                       f'<input type="hidden" name="range" value="{e(f["range"])}">'
                       f'<input type="hidden" name="q" value="{e(f["q"])}">'
                       f'{from_hidden}{to_hidden}'
                       f'<select name="project" onchange="this.form.submit()">{opts}</select></form>')
    proj_th = "<th>Project</th>" if with_project else ""
    return f"""<div class="section">
    <div class="filter-bar">
      {proj_select}
      {rbtn("1d", "1d")}{rbtn("3d", "3d")}{rbtn("7d", "7d")}{rbtn("1m", "1 month")}
      <a class="rbtn {'active' if f['range'] == 'custom' else ''}" href="#" onclick="toggleCustom(event)">Custom ▾</a>
      <form class="custom-range" id="custom-form" style="{custom_style}" method="get" action="{base_path}">
        <input type="hidden" name="range" value="custom">
        <input type="hidden" name="q" value="{e(f['q'])}">
        {hidden_proj}
        <span>From</span><input type="date" name="from_date" value="{e(custom_from)}">
        <span>To</span><input type="date" name="to_date" value="{e(custom_to)}">
        <button type="submit">Apply</button>
      </form>
      <form class="search-box" method="get" action="{base_path}">
        <input type="hidden" name="range" value="{e(f['range'])}">
        {hidden_proj}{from_hidden}{to_hidden}
        <input type="text" name="q" placeholder="Search Jira key…" value="{e(f['q'])}">
        <button type="submit">Search</button>
      </form>
    </div>
    <table id="history-table">
      <thead><tr><th>Time</th>{proj_th}<th>Direction</th><th>Event</th><th>Type</th>
        <th>Jira Key</th><th class="col-lark">Lark Record</th><th>Description</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div class="pagination">{prev_btn}
      <span class="pg-info">Page {page} of {pages} &nbsp;·&nbsp; {total} events</span>
      {next_btn}</div>
  </div>"""


# ── pages ───────────────────────────────────────────────────────────────────

def _sidebar_projects() -> list:
    import projects_api  # lazy: keeps import order simple
    return projects_api.do_list_projects()


def render_activity(request, history_result: dict) -> str:
    try:
        sel = int(request.query_params.get("project", ""))
    except ValueError:
        sel = None
    body = f"""<div class="header"><div class="header-left"><h1>All activity</h1></div></div>
<div class="main">
  <div class="cards"><div class="card"><div class="label">Total Events</div>
    <div class="value">{history_result["total"]}</div>
    <div class="sub">Matching current filter</div></div></div>
  {_history_section(request, history_result, "/activity", True, sel)}
</div>"""
    return _shell("All activity", sidebar(_sidebar_projects(), None, activity=True), body)


_PROJECT_JS = """
var PID = __PID__;
var API = '/api/p/__PID__/';
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
async function errText(res) {
  try {
    var d = await res.json();
    return d.error || d.message || d.detail || ('HTTP ' + res.status);
  } catch (x) { return 'HTTP ' + res.status; }
}

async function loadTables() {
  var picker = document.getElementById('table-picker');
  picker.style.display = 'block';
  picker.innerHTML = '<span style="color:#64748b;font-size:12px">Loading tables…</span>';
  try {
    var res = await fetch(API + 'tables');
    var data = await res.json();
    if (data.error) { picker.innerHTML = '<span style="color:#ef4444">Error: ' + esc(data.error) + '</span>'; return; }
    var html = '<div class="table-list">';
    data.tables.forEach(function(t) {
      var active = t.table_id === data.active_table_id;
      html += '<div class="table-item' + (active ? ' active-tbl' : '') + '">';
      html += '<div><div>' + esc(t.name) + (active ? ' ✓' : '') + '</div>';
      html += '<div class="tbl-id">' + esc(t.table_id) + '</div></div>';
      html += '<button ' + (active ? 'disabled' : '') + ' data-id="' + esc(t.table_id) + '" data-name="' + esc(t.name) + '" onclick="switchTable(this.dataset.id,this.dataset.name)">Select</button>';
      html += '</div>';
    });
    html += '</div>';
    picker.innerHTML = html;
  } catch(e) { picker.innerHTML = '<span style="color:#ef4444">Failed to load tables.</span>'; }
}

async function switchTable(id, name) {
  if (!confirm('Switch sync to table "' + name + '"?\\nThis will rebuild the index.')) return;
  var res = await fetch(API.slice(0, -1), {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({lark_table_id: id, lark_table_name: name})
  });
  if (res.ok) {
    document.getElementById('active-table-label').textContent = name + ' (' + id + ')';
    document.getElementById('table-picker').style.display = 'none';
    alert('Switched to "' + name + '". The index is rebuilding.');
  } else {
    alert('Error: ' + await errText(res));
  }
}

// ── Field Mappings ──
var _fmData = [];
var _larkFields = [];
var _jiraFields = [];
var DIRECTIONS = {'both':'Both','lark_to_jira':'Lark → Jira','jira_to_lark':'Jira → Lark'};
var TYPES = {'text':'Text','date':'Date','number':'Number','select':'Select','user':'User'};

async function loadFields() {
  var res = await fetch(API + 'fields');
  var data = await res.json();
  _fmData = data.mappings || [];
  renderFields();
}

async function loadAvailableFields() {
  try {
    var lr = await fetch(API + 'lark-fields');
    var ld = await lr.json();
    _larkFields = ld.fields || [];
  } catch(e) { _larkFields = []; }
  try {
    var jr = await fetch(API + 'jira-fields');
    var jd = await jr.json();
    _jiraFields = jd.fields || [];
  } catch(e) { _jiraFields = []; }
}

function renderFields() {
  var tbody = document.getElementById('field-tbody');
  if (!_fmData.length) { tbody.innerHTML = '<tr><td colspan="7" class="empty">No mappings found.</td></tr>'; return; }
  var html = '';
  _fmData.forEach(function(m) {
    html += '<tr id="fm-row-' + m.id + '">';
    html += '<td>' + esc(m.lark_field) + '</td>';
    html += '<td>' + esc(m.jira_field) + '</td>';
    html += '<td>' + esc(m.jira_label || '') + '</td>';
    html += '<td>' + esc(DIRECTIONS[m.direction] || m.direction) + '</td>';
    html += '<td>' + esc(TYPES[m.field_type] || m.field_type) + '</td>';
    html += '<td>' + (m.active ? '✓' : '—') + '</td>';
    html += '<td>';
    html += '<button class="fm-edit-btn fm-edit" onclick="editRow(' + m.id + ')">Edit</button>';
    html += '<button class="fm-edit-btn fm-delete" onclick="deleteField(' + m.id + ')">Delete</button>';
    html += '</td></tr>';
  });
  tbody.innerHTML = html;
}

function larkFieldSelect(cur) {
  var opts = '<option value="">— choose Lark field —</option>';
  _larkFields.forEach(function(f) {
    var sel = f.field_name === cur ? ' selected' : '';
    opts += '<option value="' + esc(f.field_name) + '"' + sel + '>' + esc(f.field_name) + '</option>';
  });
  if (_larkFields.length === 0) {
    opts += '<option value="' + esc(cur) + '" selected>' + esc(cur || 'Loading…') + '</option>';
  }
  return '<select class="fm-field-sel">' + opts + '</select>';
}

function jiraFieldSelect(cur) {
  var opts = '<option value="">— choose Jira field —</option>';
  var system = _jiraFields.filter(function(f) { return !f.custom; });
  var custom = _jiraFields.filter(function(f) { return f.custom; });
  function addGroup(label, arr) {
    if (!arr.length) return;
    opts += '<optgroup label="' + label + '">';
    arr.forEach(function(f) {
      var sel = f.id === cur ? ' selected' : '';
      opts += '<option value="' + esc(f.id) + '"' + sel + '>' + esc(f.name) + ' (' + esc(f.id) + ')</option>';
    });
    opts += '</optgroup>';
  }
  addGroup('System fields', system);
  addGroup('Custom fields', custom);
  if (_jiraFields.length === 0) {
    opts += '<option value="' + esc(cur) + '" selected>' + esc(cur || 'Loading…') + '</option>';
  }
  return '<select class="fm-field-sel">' + opts + '</select>';
}

function editRow(id) {
  var m = _fmData.find(function(x) { return x.id === id; });
  if (!m) return;
  var row = document.getElementById('fm-row-' + id);
  row.innerHTML =
    '<td>' + larkFieldSelect(m.lark_field) + '</td>' +
    '<td>' + jiraFieldSelect(m.jira_field) + '</td>' +
    '<td><input type="text" class="fm-label-inp" value="' + esc(m.jira_label||'') + '" placeholder="Label"></td>' +
    '<td>' + dirSelect(m.direction) + '</td>' +
    '<td>' + typeSelect(m.field_type) + '</td>' +
    '<td><input type="checkbox"' + (m.active?' checked':'') + '></td>' +
    '<td><button class="fm-edit-btn fm-save" onclick="saveRow(' + id + ')">Save</button>' +
    '<button class="fm-edit-btn fm-cancel" onclick="renderFields()">Cancel</button></td>';
}

async function postField(payload) {
  try {
    var res = await fetch(API + 'fields', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var d = await res.json();
    if (!res.ok || d.ok === false) { alert('Error: ' + (d.error || d.detail || ('HTTP ' + res.status))); return false; }
    return true;
  } catch(e) { alert('Request failed'); return false; }
}

async function saveRow(id) {
  var row = document.getElementById('fm-row-' + id);
  var lf  = row.querySelector('.fm-field-sel').value;
  var selects = row.querySelectorAll('.fm-field-sel');
  var jf  = selects[1] ? selects[1].value : '';
  var lbl = row.querySelector('.fm-label-inp') ? row.querySelector('.fm-label-inp').value.trim() : '';
  var allSel = row.querySelectorAll('select');
  var dir = allSel[2] ? allSel[2].value : 'both';
  var typ = allSel[3] ? allSel[3].value : 'text';
  var chk = row.querySelector('input[type=checkbox]');
  var m = _fmData.find(function(x) { return x.id === id; });
  if (!lf || !jf) { alert('Lark field and Jira field are required.'); return; }
  var payload = Object.assign({}, m, {
    lark_field: lf, jira_field: jf, jira_label: lbl,
    direction: dir, field_type: typ, active: chk ? chk.checked : true
  });
  if (await postField(payload)) loadFields();
}

async function deleteField(id) {
  if (!confirm('Delete this field mapping?')) return;
  try {
    var res = await fetch(API + 'fields/' + id, {method: 'DELETE'});
    var d = {};
    try { d = await res.json(); } catch(x) {}
    if (!res.ok || d.ok === false) { alert(d.error || d.message || d.detail || ('Delete failed (HTTP ' + res.status + ')')); }
  } catch(e) { alert('Request failed'); }
  loadFields();
}

function addFieldRow() {
  if (_larkFields.length === 0 || _jiraFields.length === 0) {
    loadAvailableFields().then(function() { _insertNewRow(); });
  } else {
    _insertNewRow();
  }
}

function _insertNewRow() {
  var existing = document.getElementById('fm-row-new');
  if (existing) existing.remove();
  var tbody = document.getElementById('field-tbody');
  var tr = document.createElement('tr');
  tr.className = 'new-row';
  tr.id = 'fm-row-new';
  tr.innerHTML =
    '<td>' + larkFieldSelect('') + '</td>' +
    '<td>' + jiraFieldSelect('') + '</td>' +
    '<td><input type="text" class="fm-label-inp" placeholder="Label"></td>' +
    '<td>' + dirSelect('both') + '</td>' +
    '<td>' + typeSelect('text') + '</td>' +
    '<td><input type="checkbox" checked></td>' +
    '<td><button class="fm-edit-btn fm-save" onclick="saveNewRow()">Save</button>' +
    '<button class="fm-edit-btn fm-cancel" onclick="this.closest(\\'tr\\').remove()">Cancel</button></td>';
  tbody.insertBefore(tr, tbody.firstChild);
}

async function saveNewRow() {
  var row = document.getElementById('fm-row-new');
  var selects = row.querySelectorAll('.fm-field-sel');
  var lf = selects[0] ? selects[0].value.trim() : '';
  var jf = selects[1] ? selects[1].value.trim() : '';
  var lbl = row.querySelector('.fm-label-inp') ? row.querySelector('.fm-label-inp').value.trim() : '';
  var allSel = row.querySelectorAll('select');
  var dir = allSel[2] ? allSel[2].value : 'both';
  var typ = allSel[3] ? allSel[3].value : 'text';
  var chk = row.querySelector('input[type=checkbox]');
  if (!lf || !jf) { alert('Lark field and Jira field are required.'); return; }
  var ok = await postField({
    lark_field: lf, jira_field: jf, jira_label: lbl,
    direction: dir, field_type: typ, active: chk ? chk.checked : true, is_system: false
  });
  if (ok) loadFields();
}

function dirSelect(cur) {
  return '<select>' + Object.keys(DIRECTIONS).map(function(k) {
    return '<option value="' + k + '"' + (k===cur?' selected':'') + '>' + DIRECTIONS[k] + '</option>';
  }).join('') + '</select>';
}
function typeSelect(cur) {
  return '<select>' + Object.keys(TYPES).map(function(k) {
    return '<option value="' + k + '"' + (k===cur?' selected':'') + '>' + TYPES[k] + '</option>';
  }).join('') + '</select>';
}

// ── Sync Types ──
async function loadSyncTypes() {
  try {
    var results = await Promise.all([
      fetch(API + 'sync-types').then(function(r) { return r.json(); }),
      fetch(API + 'jira-issue-types').then(function(r) { return r.json(); }),
      fetch(API + 'lark-type-options').then(function(r) { return r.json(); }),
    ]);
    renderTypeCheckboxes('jira-types-list', results[1].types || [], results[0].allowed_jira || []);
    renderTypeCheckboxes('lark-types-list', results[2].options || [], results[0].allowed_lark || []);
  } catch(e) {
    document.getElementById('jira-types-list').innerHTML = '<span style="color:#ef4444">Failed to load</span>';
    document.getElementById('lark-types-list').innerHTML = '<span style="color:#ef4444">Failed to load</span>';
  }
}

function renderTypeCheckboxes(containerId, allOptions, checked) {
  var el = document.getElementById(containerId);
  if (!allOptions.length) {
    el.innerHTML = '<span style="color:#94a3b8;font-size:12px">No options found.</span>';
    return;
  }
  var html = '';
  allOptions.forEach(function(opt) {
    var isChecked = checked.indexOf(opt) !== -1 ? ' checked' : '';
    html += '<label><input type="checkbox" data-val="' + esc(opt) + '"' + isChecked + '> ' + esc(opt) + '</label>';
  });
  el.innerHTML = html;
}

async function saveSyncTypes() {
  var jiraChecked = Array.from(document.querySelectorAll('#jira-types-list input[type=checkbox]:checked'))
    .map(function(cb) { return cb.dataset.val; });
  var larkChecked = Array.from(document.querySelectorAll('#lark-types-list input[type=checkbox]:checked'))
    .map(function(cb) { return cb.dataset.val; });
  var msg = document.getElementById('sync-save-msg');
  msg.textContent = 'Saving…';
  msg.style.color = '#64748b';
  try {
    var res = await fetch(API + 'sync-types', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({jira_types: jiraChecked, lark_types: larkChecked})
    });
    var data = await res.json();
    if (data.ok) { msg.textContent = 'Saved!'; msg.style.color = '#22c55e'; }
    else { msg.textContent = 'Error: ' + (data.error || 'unknown'); msg.style.color = '#ef4444'; }
  } catch(e) { msg.textContent = 'Request failed'; msg.style.color = '#ef4444'; }
  setTimeout(function() { msg.textContent = ''; }, 3000);
}

// ── People ──
var _unmappedJira = [];
// An ignored person has Lark name "" in the map: their Jira assignment is
// never synced to Lark (Lark Assignee left unchanged, nothing auto-added).
function peopleRow(j, l, ign) {
  return '<tr><td><input type="text" class="pp-jira" value="' + esc(j) + '" placeholder="Jira display name"></td>' +
    '<td><input type="text" class="pp-lark" value="' + (ign ? '' : esc(l)) + '" placeholder="' +
      (ign ? '(ignored)' : 'Lark name') + '"' + (ign ? ' disabled' : '') + '>' +
      '<span class="pp-ign-tag"' + (ign ? '' : ' style="display:none"') + '> (ignored)</span></td>' +
    '<td><label><input type="checkbox" class="pp-ign" onchange="ppIgnore(this)"' + (ign ? ' checked' : '') + '> Ignore</label></td>' +
    '<td><button class="fm-edit-btn fm-delete" onclick="this.closest(\\'tr\\').remove()">Remove</button></td></tr>';
}
function ppIgnore(cb) {
  var tr = cb.closest('tr'), inp = tr.querySelector('.pp-lark');
  inp.disabled = cb.checked;
  inp.placeholder = cb.checked ? '(ignored)' : 'Lark name';
  if (cb.checked) inp.value = '';
  tr.querySelector('.pp-ign-tag').style.display = cb.checked ? '' : 'none';
}
function addPeopleRow(j, l) {
  document.getElementById('people-tbody').insertAdjacentHTML('beforeend', peopleRow(j || '', l || '', false));
}
// rows: [{j, l, ign}] -> {map, missing}. Ignored -> "", a named row -> its
// name, a row with a Jira name but no Lark name and not ignored -> missing
// (never silently dropped). Rows with no Jira name are blank and skipped.
function collectPeople(rows) {
  var map = {}, missing = [];
  rows.forEach(function(r) {
    var j = (r.j || '').trim(), l = (r.l || '').trim();
    if (!j) return;
    if (r.ign) map[j] = '';
    else if (l) map[j] = l;
    else missing.push(j);
  });
  return {map: map, missing: missing};
}
async function loadPeople() {
  var tb = document.getElementById('people-tbody');
  try {
    var d = await (await fetch(API + 'people')).json();
    var m = d.assignee_map || {};
    tb.innerHTML = Object.keys(m).sort().map(function(k) {
      return peopleRow(k, m[k], !String(m[k] || '').trim());
    }).join('');
  } catch(e) { tb.innerHTML = '<tr><td colspan="4" class="empty">Failed to load.</td></tr>'; }
  loadPeopleCheck();
}
async function loadPeopleCheck() {
  var b = document.getElementById('people-badge');
  try {
    var d = await (await fetch(API + 'check')).json();
    _unmappedJira = (d.people && d.people.unmapped_jira) || [];
    if (_unmappedJira.length) {
      b.className = 'unmapped-badge';
      b.textContent = _unmappedJira.length + ' unmapped';
      b.title = _unmappedJira.join(', ');
      document.getElementById('people-add-unmapped').style.display = 'inline-block';
    } else {
      b.className = 'unmapped-badge ok';
      b.textContent = d.people_error ? 'check unavailable' : 'all mapped';
      b.title = d.people_error || '';
    }
  } catch(e) { b.textContent = ''; }
}
function addUnmapped() {
  var have = Array.from(document.querySelectorAll('.pp-jira')).map(function(i) { return i.value.trim(); });
  _unmappedJira.forEach(function(n) { if (have.indexOf(n) === -1) addPeopleRow(n, ''); });
}
async function savePeople() {
  var rows = Array.from(document.querySelectorAll('#people-tbody tr')).filter(function(r) {
    return r.querySelector('.pp-jira');
  }).map(function(r) {
    return {j: r.querySelector('.pp-jira').value, l: r.querySelector('.pp-lark').value,
            ign: r.querySelector('.pp-ign').checked};
  });
  var c = collectPeople(rows), m = c.map, missing = c.missing;
  var msg = document.getElementById('people-msg');
  if (missing.length) {
    msg.textContent = 'Lark name missing for: ' + missing.join(', ') + '. Enter a name or tick Ignore.';
    msg.style.color = '#ef4444';
    return;
  }
  msg.textContent = 'Saving…'; msg.style.color = '#64748b';
  try {
    var res = await fetch(API + 'people', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({assignee_map: m})
    });
    if (res.ok) { msg.textContent = 'Saved!'; msg.style.color = '#22c55e'; loadPeopleCheck(); }
    else { msg.textContent = 'Error: ' + await errText(res); msg.style.color = '#ef4444'; }
  } catch(e) { msg.textContent = 'Request failed'; msg.style.color = '#ef4444'; }
  setTimeout(function() { msg.textContent = ''; }, 3000);
}

// ── Backfill ──
async function runBackfill() {
  var btn = document.getElementById('backfill-btn');
  var msg = document.getElementById('backfill-msg');
  var results = document.getElementById('backfill-results');
  btn.disabled = true;
  msg.textContent = 'Running… (this may take a moment)';
  msg.style.color = '#94a3b8';
  results.style.display = 'none';
  results.innerHTML = '';
  try {
    var res = await fetch(API + 'backfill', { method: 'POST' });
    var data = await res.json();
    if (data.error) {
      msg.textContent = 'Error: ' + data.error;
      msg.style.color = '#ef4444';
    } else {
      var stats = [];
      if (data.removed_duplicates > 0) stats.push(data.removed_duplicates + ' duplicates removed');
      if (data.matched > 0)            stats.push(data.matched + ' matched by title');
      if (data.created_lark > 0)       stats.push(data.created_lark + ' Lark records created');
      if (data.created_jira > 0)       stats.push(data.created_jira + ' Jira issues created');
      if (data.synced > 0)             stats.push(data.synced + ' records synced');
      if (data.skipped_ambiguous > 0)  stats.push(data.skipped_ambiguous + ' skipped (ambiguous title)');
      var anyAction = data.removed_duplicates + data.matched + data.created_lark + data.created_jira + data.synced;
      msg.textContent = anyAction > 0 ? stats.join(' · ') : 'Nothing to do — all records already in sync';
      msg.style.color = anyAction > 0 ? '#16a34a' : '#94a3b8';
      if (data.pairs && data.pairs.length > 0) {
        var rows = data.pairs.map(function(p) {
          return '<tr><td style="padding:3px 12px 3px 0;font-family:monospace;color:#2563eb;font-size:12px">' + esc(p.jira_key) + '</td>'
            + '<td style="padding:3px 0;font-size:12px">' + esc(p.title) + '</td></tr>';
        }).join('');
        results.innerHTML = '<div style="color:#64748b;font-size:11px;margin-bottom:4px">Matched by title:</div><table>' + rows + '</table>';
        results.style.display = 'block';
      }
    }
  } catch(e) { msg.textContent = 'Request failed'; msg.style.color = '#ef4444'; }
  btn.disabled = false;
}

document.addEventListener('DOMContentLoaded', function() {
  loadFields();
  loadAvailableFields();
  loadSyncTypes();
  loadPeople();
});
"""


def render_project(p, request, history_result: dict) -> str:
    pid = int(p.id)
    draft = p.setup_state == "draft"
    sync_on = p.sync_enabled
    status_color = "#94a3b8" if draft else ("#22c55e" if sync_on else "#f59e0b")
    status_text = "Draft" if draft else ("Live" if sync_on else "Paused")
    status_sub = ("Setup not finished" if draft else
                  "Sync active" if sync_on else "Webhooks received but not processed")
    toggle_label = "Disable Sync" if sync_on else "Enable Sync"
    toggle_color = "#ef4444" if sync_on else "#22c55e"
    rec_label = "Disable Reconcile" if p.reconcile_enabled else "Enable Reconcile"
    rec_color = "#f59e0b" if p.reconcile_enabled else "#22c55e"
    try:
        jira_domain = projects.cfg_for(p)["JIRA_DOMAIN"]
    except KeyError:
        jira_domain = os.environ.get("JIRA_DOMAIN", "")
    table_label = (f"{p.lark_table_name} ({p.lark_table_id})" if p.lark_table_name
                   else p.lark_table_id)
    name = p.name or p.jira_project_key

    banners = ""
    if draft:
        banners += (f'<div class="draft-banner">This project is a draft — sync is off until setup is '
                    f'finished. <a href="/p/{pid}/setup">Continue setup →</a></div>')
    elif not sync_on:
        banners += ("<div class='paused-banner'>⚠ Sync is paused — webhooks are received but "
                    "changes are NOT propagated.</div>")

    body = f"""<div class="header">
  <div class="header-left">
    <div class="dot" style="background:{status_color};box-shadow:0 0 0 3px {status_color}44"></div>
    <h1>{e(name)}</h1><span class="key">{e(p.jira_project_key)}</span>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <form method="post" action="/p/{pid}/toggle">
      <button class="toggle-btn" style="background:{toggle_color}" type="submit">{toggle_label}</button>
    </form>
    <form method="post" action="/p/{pid}/toggle/reconcile">
      <button class="toggle-btn" style="background:{rec_color}" type="submit">{rec_label}</button>
    </form>
  </div>
</div>
<div class="main">
  {banners}
  <div class="cards">
    <div class="card"><div class="label">Status</div>
      <div class="value" style="color:{status_color}">{status_text}</div>
      <div class="sub">{status_sub}</div></div>
    <div class="card"><div class="label">Linked Records</div>
      <div class="value">{index.count(pid)}</div>
      <div class="sub">Jira ↔ Lark pairs in index</div></div>
    <div class="card"><div class="label">Total Events</div>
      <div class="value">{history_result["total"]}</div>
      <div class="sub">Matching current filter</div></div>
  </div>

  <div class="section">
    <div class="section-header">Configuration</div>
    <table>
      <tr><td class="cfg-key">Jira Domain</td><td class="cfg-val">{e(jira_domain)}</td></tr>
      <tr><td class="cfg-key">Jira Project</td><td class="cfg-val">{e(p.jira_project_key)}</td></tr>
      <tr><td class="cfg-key">Lark Base Token</td><td class="cfg-val">{e(p.lark_base_token)}</td></tr>
      <tr><td class="cfg-key">Lark Table</td>
          <td class="cfg-val"><span id="active-table-label">{e(table_label)}</span>
            <button class="change-btn" onclick="loadTables()">Change Table</button>
            <div id="table-picker" style="display:none;margin-top:8px"></div></td></tr>
      <tr><td class="cfg-key">Jira Webhook URL</td><td class="cfg-val">{e(_cfg_mod.PUBLIC_URL)}/webhook/jira</td></tr>
      <tr><td class="cfg-key">Lark Webhook URL</td><td class="cfg-val">{e(_cfg_mod.PUBLIC_URL)}/webhook/lark-auto/{pid}</td></tr>
    </table>
  </div>

  <div class="section" id="field-mappings-section">
    <div class="section-header" style="display:flex;align-items:center;justify-content:space-between">
      <span>Field Mappings</span>
      <button class="change-btn" onclick="addFieldRow()">+ Add Field</button>
    </div>
    <table id="field-table">
      <thead><tr><th>Lark Field</th><th>Jira Field</th><th>Label</th><th>Direction</th>
        <th>Type</th><th>Active</th><th>Actions</th></tr></thead>
      <tbody id="field-tbody"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>

  <div class="section" id="people-section">
    <div class="section-header" style="display:flex;align-items:center;justify-content:space-between">
      <span>People<span class="unmapped-badge ok" id="people-badge"></span></span>
      <span><button class="change-btn" id="people-add-unmapped" style="display:none" onclick="addUnmapped()">Add unmapped</button>
        <button class="change-btn" onclick="addPeopleRow()">+ Add person</button></span>
    </div>
    <table id="people-table">
      <thead><tr><th>Jira name</th><th>Lark name</th><th>Ignore</th><th></th></tr></thead>
      <tbody id="people-tbody"><tr><td colspan="4" class="empty">Loading…</td></tr></tbody>
    </table>
    <div class="sync-save-row">
      <button class="sync-save-btn" onclick="savePeople()">Save People</button>
      <span class="sync-msg" id="people-msg"></span>
    </div>
  </div>

  <div class="section" id="sync-types-section">
    <div class="section-header">Sync Types</div>
    <div class="sync-types-panels">
      <div class="sync-panel"><h3>Jira Issue Types</h3>
        <div id="jira-types-list"><span style="color:#94a3b8;font-size:12px">Loading…</span></div></div>
      <div class="sync-panel"><h3>Lark Type Field Values</h3>
        <div id="lark-types-list"><span style="color:#94a3b8;font-size:12px">Loading…</span></div></div>
    </div>
    <div class="sync-save-row">
      <button class="sync-save-btn" onclick="saveSyncTypes()">Save Sync Types</button>
      <span class="sync-msg" id="sync-save-msg"></span>
    </div>
  </div>

  <div class="section" id="backfill-section">
    <div class="section-header">Backfill Pre-existing Records</div>
    <div style="padding:12px 20px 4px;color:#94a3b8;font-size:13px">
      For records/issues that existed before the webhook was set up.
      Removes duplicate Lark records, matches by title, creates missing records on both sides, and syncs field values.
      Only processes types configured above.
    </div>
    <div class="sync-save-row">
      <button class="sync-save-btn" id="backfill-btn" onclick="runBackfill()">Run Backfill</button>
      <span class="sync-msg" id="backfill-msg"></span>
    </div>
    <div id="backfill-results" style="display:none;padding:0 20px 16px"></div>
  </div>

  {_history_section(request, history_result, f"/p/{pid}", False)}
</div>"""
    script = "<script>" + _PROJECT_JS.replace("__PID__", str(pid)) + "</script>"
    return _shell(f"{name} · Jira ↔ Lark Sync",
                  sidebar(_sidebar_projects(), pid), body, script)
