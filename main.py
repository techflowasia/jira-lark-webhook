"""FastAPI entry point: / dashboard + /health + /webhook/lark + /webhook/jira + reconcile loop."""
import asyncio
import json
import logging
import urllib.request
import urllib.parse
from collections import deque
from datetime import datetime
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
import lark_api, index, reconcile, history
import lark_handler, jira_handler
from config import get_cfg, set_active_table

LARK_APP_ID     = "cli_a9772fc461e1de15"
LARK_APP_SECRET = "c8umFVp63U25n9USaljMjeKOOAp0uenw"
LARK_BASE_TOKEN = "DdwQbYcA3aMpeKs6gTcjk7n2pnf"
BOT_OPEN_ID     = "ou_6c4fb657f0b844228990210a8fc789b5"
REDIRECT_URI    = "https://jira-lark-webhook.onrender.com/auth/callback"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI()

# Global enable/disable flag
_sync_enabled: bool = True

# Store last 20 raw payloads for debugging
_raw_payloads: deque = deque(maxlen=20)


@app.on_event("startup")
async def startup() -> None:
    cfg = get_cfg()
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
    index.rebuild(records)
    logging.getLogger(__name__).info(f"Index built: {len(index._jira_to_lark)} linked records")
    asyncio.create_task(_reconcile_loop())
    asyncio.create_task(_keepalive_loop())
    # Load active table from Supabase in background (non-blocking)
    asyncio.create_task(_load_active_table_async())


async def _load_active_table_async() -> None:
    """Load active table from Supabase settings in a background thread, re-index if changed."""
    import os as _os
    try:
        def _fetch():
            client = history._get_client()
            if not client:
                return None, None
            rows = client.table("settings").select("key,value") \
                .in_("key", ["active_table_id", "active_table_name"]).execute()
            kv = {r["key"]: r["value"] for r in (rows.data or [])}
            return (kv.get("active_table_id") or _os.environ.get("LARK_TABLE_ID", ""),
                    kv.get("active_table_name") or "")

        tid, name = await asyncio.to_thread(_fetch)
        if tid and tid != get_cfg()["LARK_TABLE_ID"]:
            # Table differs from env var — switch and rebuild index
            set_active_table(tid, name)
            cfg = get_cfg()
            token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
            records = await asyncio.to_thread(
                lark_api.fetch_all_records, token, cfg["LARK_BASE_TOKEN"], tid)
            index.rebuild(records)
            logging.getLogger(__name__).info(
                f"Active table loaded from DB: '{name}' ({tid}), {len(index._jira_to_lark)} records")
        elif tid:
            set_active_table(tid, name)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not load active table from DB: {e}")


async def _keepalive_loop() -> None:
    """Ping own /health every 5 min to prevent Render free-tier spindown."""
    import os as _os
    port = int(_os.environ.get("PORT", 10000))
    while True:
        await asyncio.sleep(300)
        try:
            urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5)
        except Exception:
            pass


async def _reconcile_loop() -> None:
    while True:
        await asyncio.sleep(1800)
        await asyncio.to_thread(reconcile.run, get_cfg())


@app.get("/auth/start")
async def auth_start():
    """Step 1 — redirect user to Lark OAuth to get a user access token."""
    params = urllib.parse.urlencode({
        "app_id":       LARK_APP_ID,
        "redirect_uri": REDIRECT_URI,
        "scope":        "drive:drive",
        "state":        "add_bot",
    })
    return RedirectResponse(f"https://open.larksuite.com/open-apis/authen/v1/authorize?{params}")


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request):
    """Step 2 — exchange code for user token, add bot as editor on the Base."""
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h2>Error: no code in callback</h2>", status_code=400)

    # Step 1 — get app_access_token
    r0 = urllib.request.Request(
        "https://open.larksuite.com/open-apis/auth/v3/app_access_token/internal",
        data=json.dumps({"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    app_token = json.loads(urllib.request.urlopen(r0).read())["app_access_token"]

    # Step 2 — exchange auth code for user access token
    req = urllib.request.Request(
        "https://open.larksuite.com/open-apis/authen/v1/access_token",
        data=json.dumps({"grant_type": "authorization_code", "code": code}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {app_token}"},
        method="POST",
    )
    token_resp = json.loads(urllib.request.urlopen(req).read())
    user_token = token_resp.get("data", {}).get("access_token")
    if not user_token:
        return HTMLResponse(f"<h2>Token exchange failed</h2><pre>{json.dumps(token_resp, indent=2)}</pre>", status_code=400)

    # Add bot as editor on the Base using the user's token
    req2 = urllib.request.Request(
        f"https://open.larksuite.com/open-apis/drive/v1/permissions/{LARK_BASE_TOKEN}/members?type=bitable",
        data=json.dumps({
            "member_type": "openid",
            "member_id":   BOT_OPEN_ID,
            "perm":        "edit",
            "perm_type":   "container",
            "notify_lark": False,
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {user_token}"},
        method="POST",
    )
    perm_ok = False
    perm_msg = ""
    try:
        perm_resp = json.loads(urllib.request.urlopen(req2).read())
        perm_ok = perm_resp.get("code") == 0
        perm_msg = "Bot added as editor" if perm_ok else f"Permission API: {perm_resp.get('msg')} (code {perm_resp.get('code')})"
    except Exception as e:
        perm_msg = f"Permission API exception: {e}"

    # Subscribe to bitable record-change events using the user token
    sub_ok = False
    sub_msg = ""
    try:
        req3 = urllib.request.Request(
            f"https://open.larksuite.com/open-apis/drive/v1/files/{LARK_BASE_TOKEN}/subscribe?file_type=bitable",
            data=json.dumps({"event_types": ["drive.file.bitable_record_changed_v1"]}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {user_token}"},
            method="POST",
        )
        sub_resp = json.loads(urllib.request.urlopen(req3).read())
        sub_ok = sub_resp.get("code") == 0
        sub_msg = "Subscribed to record-change events!" if sub_ok else f"Subscribe API: {sub_resp.get('msg')} (code {sub_resp.get('code')})"
    except Exception as e:
        sub_msg = f"Subscribe API exception: {e}"

    ok = perm_ok and sub_ok
    color = "#22c55e" if ok else ("#f59e0b" if (perm_ok or sub_ok) else "#ef4444")
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;padding:40px;max-width:500px;margin:auto">
    <h2 style="color:{color}">{"✅ Setup complete!" if ok else "⚠️ Partial setup"}</h2>
    <p>{"✅" if perm_ok else "❌"} {perm_msg}</p>
    <p>{"✅" if sub_ok else "❌"} {sub_msg}</p>
    <p>{"Lark → Jira sync is now active. Edit any Lark record to test." if ok else "Check errors above and try again."}</p>
    <p><a href="/">← Back to dashboard</a></p>
    </body></html>
    """)


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode()).decode()


@app.get("/health")
async def health():
    return {"ok": True, "sync_enabled": _sync_enabled}


@app.get("/api/tables")
async def api_tables():
    """List all tables in the Lark Base."""
    try:
        cfg = get_cfg()
        token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
        tables = lark_api.list_tables(token, cfg["LARK_BASE_TOKEN"])
        return {"tables": tables, "active_table_id": cfg["LARK_TABLE_ID"]}
    except Exception as e:
        return {"error": str(e), "tables": []}


@app.post("/settings/table")
async def set_table(request: Request):
    """Switch the active Lark table and rebuild the index."""
    body = await request.json()
    table_id   = body.get("table_id", "").strip()
    table_name = body.get("name", "").strip()
    if not table_id:
        return {"ok": False, "error": "table_id required"}

    set_active_table(table_id, table_name)

    # Persist to Supabase settings
    client = history._get_client()
    if client:
        try:
            client.table("settings").upsert({"key": "active_table_id",   "value": table_id}).execute()
            client.table("settings").upsert({"key": "active_table_name", "value": table_name}).execute()
        except Exception as e:
            logging.getLogger(__name__).warning(f"Could not persist table setting: {e}")

    # Rebuild index for the new table
    cfg = get_cfg()
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], table_id)
    index.rebuild(records)
    logging.getLogger(__name__).info(f"Switched to table '{table_name}' ({table_id}), index rebuilt: {len(index._jira_to_lark)} records")
    history.record(direction="system", event="config",
                   description=f"Switched Lark table to '{table_name}' ({table_id})")
    return {"ok": True, "table_id": table_id, "name": table_name, "linked": len(index._jira_to_lark)}


@app.post("/toggle")
async def toggle_sync():
    global _sync_enabled
    _sync_enabled = not _sync_enabled
    state = "enabled" if _sync_enabled else "disabled"
    logging.getLogger(__name__).info(f"Sync {state} via dashboard toggle")
    history.record(direction="system", event="config",
                   description=f"Sync {state} via dashboard")
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from datetime import timezone, timedelta
    cfg = get_cfg()
    linked = len(index._jira_to_lark)

    # --- Parse filter params ---
    range_param  = request.query_params.get("range", "1d")
    from_date_str = request.query_params.get("from_date", "")
    to_date_str   = request.query_params.get("to_date", "")
    q    = request.query_params.get("q", "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1") or "1"))
    except ValueError:
        page = 1

    now = datetime.now(timezone.utc)
    range_days = {"1d": 1, "3d": 3, "7d": 7, "1m": 30}

    if range_param == "custom":
        try:
            from_dt = datetime.fromisoformat(from_date_str).replace(tzinfo=timezone.utc) if from_date_str else None
        except ValueError:
            from_dt = None
        try:
            to_dt = datetime.fromisoformat(to_date_str).replace(tzinfo=timezone.utc) if to_date_str else None
        except ValueError:
            to_dt = None
    elif range_param in range_days:
        from_dt = now - timedelta(days=range_days[range_param])
        to_dt   = now
    else:
        range_param = "1d"
        from_dt = now - timedelta(days=1)
        to_dt   = now

    result = history.query(from_dt=from_dt, to_dt=to_dt, jira_key=q, page=page)
    logs   = result["rows"]
    total  = result["total"]
    pages  = result["pages"]

    # --- Helper renderers ---
    def row_class(entry):
        if entry["direction"] == "system": return "sys"
        s = entry["status"]
        if s == "error":   return "error"
        if s == "skipped": return "skip"
        return ""

    def direction_badge(d):
        if d == "system": return '<span class="badge sys">System</span>'
        if "lark" in d.split("→")[0]: return '<span class="badge lark">Lark → Jira</span>'
        return '<span class="badge jira">Jira → Lark</span>'

    def event_badge(e):
        colors = {"created": "#22c55e", "updated": "#3b82f6",
                  "deleted": "#ef4444", "config": "#a855f7"}
        c = colors.get(e, "#888")
        return f'<span class="evbadge" style="background:{c}">{e}</span>'

    rows_html = ""
    for entry in logs:
        rc  = row_class(entry)
        err = f'<div class="errmsg">{entry["error"]}</div>' if entry.get("error") else ""
        rows_html += f"""
        <tr class="{rc}">
          <td class="ts">{entry["ts"]}</td>
          <td>{direction_badge(entry["direction"])}</td>
          <td>{event_badge(entry["event"])}</td>
          <td><code>{entry.get("jira_key") or "—"}</code></td>
          <td><code class="small">{entry.get("lark_id") or "—"}</code></td>
          <td>{entry["description"]}{err}</td>
        </tr>"""
    if not rows_html:
        rows_html = '<tr><td colspan="6" class="empty">No events in this range.</td></tr>'

    # --- Pagination links ---
    def page_url(p):
        params = dict(request.query_params)
        params["page"] = str(p)
        return "/?" + urllib.parse.urlencode(params)

    prev_btn = (f'<a class="pg-btn" href="{page_url(page-1)}">← Prev</a>'
                if page > 1 else '<span class="pg-btn disabled">← Prev</span>')
    next_btn = (f'<a class="pg-btn" href="{page_url(page+1)}">Next →</a>'
                if page < pages else '<span class="pg-btn disabled">Next →</span>')
    pagination = f"""
    <div class="pagination">
      {prev_btn}
      <span class="pg-info">Page {page} of {pages} &nbsp;·&nbsp; {total} events</span>
      {next_btn}
    </div>"""

    # --- Range button helper ---
    def range_url(r):
        params = {"range": r, "q": q, "page": "1"}
        return "/?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})

    def rbtn(r, label):
        active = "active" if range_param == r else ""
        return f'<a class="rbtn {active}" href="{range_url(r)}">{label}</a>'

    custom_style = "display:flex" if range_param == "custom" else "display:none"
    custom_from  = from_date_str or (now - timedelta(days=7)).strftime("%Y-%m-%d")
    custom_to    = to_date_str   or now.strftime("%Y-%m-%d")

    toggle_label = "Disable Sync" if _sync_enabled else "Enable Sync"
    toggle_color = "#ef4444" if _sync_enabled else "#22c55e"
    status_color = "#22c55e" if _sync_enabled else "#f59e0b"
    status_text  = "Live" if _sync_enabled else "Paused"
    status_sub   = "Sync active" if _sync_enabled else "Webhooks received but not processed"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jira ↔ Lark Webhook</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f1f5f9; color: #1e293b; }}
  .header {{ background: #0f172a; color: #f8fafc; padding: 20px 32px;
             display: flex; align-items: center; justify-content: space-between; }}
  .header-left {{ display: flex; align-items: center; gap: 12px; }}
  .header h1 {{ font-size: 20px; font-weight: 600; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; background: {status_color};
          box-shadow: 0 0 0 3px {status_color}44; }}
  .toggle-btn {{ background: {toggle_color}; color: #fff; border: none; cursor: pointer;
                 padding: 8px 18px; border-radius: 6px; font-size: 13px; font-weight: 600;
                 text-decoration: none; display: inline-block; }}
  .toggle-btn:hover {{ opacity: 0.88; }}
  .main {{ padding: 24px 32px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr));
            gap: 16px; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 20px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card .label {{ font-size: 11px; font-weight: 600; text-transform: uppercase;
                  letter-spacing: .05em; color: #64748b; margin-bottom: 4px; }}
  .card .value {{ font-size: 22px; font-weight: 700; color: #0f172a; }}
  .card .sub {{ font-size: 12px; color: #64748b; margin-top: 2px; word-break: break-all; }}
  .section {{ background: #fff; border-radius: 10px;
              box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; margin-bottom: 24px; }}
  .section-header {{ padding: 14px 20px; border-bottom: 1px solid #e2e8f0;
                     font-weight: 600; font-size: 14px; background: #f8fafc; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ padding: 10px 14px; text-align: left; font-size: 11px; font-weight: 600;
        text-transform: uppercase; letter-spacing: .04em; color: #64748b;
        border-bottom: 1px solid #e2e8f0; background: #f8fafc; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.error td {{ background: #fff5f5; }}
  tr.skip  td {{ background: #fafaf0; color: #888; }}
  tr.sys   td {{ background: #faf5ff; color: #7c3aed; }}
  .ts {{ color: #64748b; font-size: 11px; white-space: nowrap; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
            font-size: 11px; font-weight: 600; white-space: nowrap; }}
  .badge.lark {{ background: #eff6ff; color: #1d4ed8; }}
  .badge.jira {{ background: #f0fdf4; color: #15803d; }}
  .badge.sys  {{ background: #faf5ff; color: #7c3aed; }}
  .evbadge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
              font-size: 11px; font-weight: 600; color: #fff; }}
  code {{ background: #f1f5f9; padding: 1px 5px; border-radius: 3px;
          font-size: 12px; font-family: monospace; }}
  code.small {{ font-size: 10px; }}
  .errmsg {{ color: #dc2626; font-size: 11px; margin-top: 3px; font-family: monospace; }}
  .empty {{ text-align: center; color: #94a3b8; padding: 40px !important; }}
  .cfg-key {{ color: #64748b; font-size: 12px; width: 160px; }}
  .cfg-val {{ font-family: monospace; font-size: 12px; }}
  .paused-banner {{ background: #fef3c7; border: 1px solid #f59e0b; border-radius: 8px;
                    padding: 12px 18px; margin-bottom: 20px; color: #92400e;
                    font-size: 13px; font-weight: 500; }}
  /* Filter bar */
  .filter-bar {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
                 padding: 12px 20px; border-bottom: 1px solid #e2e8f0; background: #f8fafc; }}
  .rbtn {{ padding: 5px 14px; border-radius: 20px; font-size: 12px; font-weight: 600;
           text-decoration: none; color: #64748b; background: #e2e8f0; border: none; cursor: pointer; }}
  .rbtn:hover {{ background: #cbd5e1; }}
  .rbtn.active {{ background: #0f172a; color: #fff; }}
  .custom-range {{ align-items: center; gap: 6px; font-size: 12px; color: #64748b; }}
  .custom-range input[type=date] {{ padding: 4px 8px; border: 1px solid #cbd5e1;
    border-radius: 6px; font-size: 12px; color: #1e293b; }}
  .custom-range button {{ padding: 5px 12px; background: #0f172a; color: #fff;
    border: none; border-radius: 6px; font-size: 12px; cursor: pointer; }}
  .search-box {{ margin-left: auto; display: flex; gap: 6px; }}
  .search-box input {{ padding: 5px 10px; border: 1px solid #cbd5e1; border-radius: 6px;
    font-size: 12px; width: 180px; }}
  .search-box button {{ padding: 5px 12px; background: #0f172a; color: #fff;
    border: none; border-radius: 6px; font-size: 12px; cursor: pointer; }}
  /* Pagination */
  .pagination {{ display: flex; align-items: center; justify-content: center; gap: 12px;
                 padding: 16px 20px; border-top: 1px solid #e2e8f0; }}
  .pg-btn {{ padding: 6px 16px; border-radius: 6px; font-size: 13px; font-weight: 600;
             text-decoration: none; background: #0f172a; color: #fff; }}
  .pg-btn.disabled {{ background: #e2e8f0; color: #94a3b8; cursor: not-allowed; pointer-events: none; }}
  .pg-info {{ font-size: 13px; color: #64748b; }}
  /* Table picker */
  .change-btn {{ margin-left: 10px; padding: 3px 10px; font-size: 11px; font-weight: 600;
                 background: #e2e8f0; border: none; border-radius: 4px; cursor: pointer; color: #1e293b; }}
  .change-btn:hover {{ background: #cbd5e1; }}
  .table-list {{ display: flex; flex-direction: column; gap: 6px; max-width: 420px; }}
  .table-item {{ display: flex; align-items: center; justify-content: space-between;
                 padding: 8px 12px; border: 1px solid #e2e8f0; border-radius: 6px;
                 background: #f8fafc; font-size: 13px; }}
  .table-item.active-tbl {{ border-color: #0f172a; background: #f0f9ff; font-weight: 600; }}
  .table-item button {{ padding: 3px 10px; font-size: 11px; font-weight: 600; border: none;
                        border-radius: 4px; cursor: pointer; background: #0f172a; color: #fff; }}
  .table-item button:disabled {{ background: #94a3b8; cursor: not-allowed; }}
  .tbl-id {{ font-size: 10px; color: #94a3b8; font-family: monospace; }}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <div class="dot"></div>
    <h1>Jira ↔ Lark Webhook Sync</h1>
  </div>
  <form method="post" action="/toggle">
    <button class="toggle-btn" type="submit">{toggle_label}</button>
  </form>
</div>
<div class="main">

  {"<div class='paused-banner'>⚠ Sync is paused — webhooks are received but changes are NOT propagated.</div>" if not _sync_enabled else ""}

  <div class="cards">
    <div class="card">
      <div class="label">Status</div>
      <div class="value" style="color:{status_color}">{status_text}</div>
      <div class="sub">{status_sub}</div>
    </div>
    <div class="card">
      <div class="label">Linked Records</div>
      <div class="value">{linked}</div>
      <div class="sub">Jira ↔ Lark pairs in index</div>
    </div>
    <div class="card">
      <div class="label">Total Events</div>
      <div class="value">{total}</div>
      <div class="sub">Matching current filter</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">Configuration</div>
    <table>
      <tr><td class="cfg-key">Jira Domain</td>
          <td class="cfg-val">{cfg["JIRA_DOMAIN"]}</td></tr>
      <tr><td class="cfg-key">Jira Project</td>
          <td class="cfg-val">{cfg["JIRA_PROJECT"]}</td></tr>
      <tr><td class="cfg-key">Lark Base Token</td>
          <td class="cfg-val">{cfg["LARK_BASE_TOKEN"]}</td></tr>
      <tr><td class="cfg-key">Lark Table</td>
          <td class="cfg-val">
            <span id="active-table-label">{cfg["LARK_TABLE_ID"]}</span>
            <button class="change-btn" onclick="loadTables()">Change Table</button>
            <div id="table-picker" style="display:none;margin-top:8px"></div>
          </td></tr>
      <tr><td class="cfg-key">Jira Webhook URL</td>
          <td class="cfg-val">https://jira-lark-webhook.onrender.com/webhook/jira</td></tr>
      <tr><td class="cfg-key">Lark Webhook URL</td>
          <td class="cfg-val">https://jira-lark-webhook.onrender.com/webhook/lark</td></tr>
    </table>
  </div>

  <div class="section">
    <!-- Filter bar -->
    <div class="filter-bar">
      {rbtn("1d", "1d")}
      {rbtn("3d", "3d")}
      {rbtn("7d", "7d")}
      {rbtn("1m", "1 month")}
      <a class="rbtn {'active' if range_param == 'custom' else ''}"
         href="#" onclick="toggleCustom(event)">Custom ▾</a>

      <form class="custom-range" id="custom-form" style="{custom_style}"
            method="get" action="/">
        <input type="hidden" name="range" value="custom">
        <input type="hidden" name="q" value="{q}">
        <span>From</span>
        <input type="date" name="from_date" value="{custom_from}">
        <span>To</span>
        <input type="date" name="to_date" value="{custom_to}">
        <button type="submit">Apply</button>
      </form>

      <form class="search-box" method="get" action="/">
        <input type="hidden" name="range" value="{range_param}">
        {'<input type="hidden" name="from_date" value="' + from_date_str + '">' if from_date_str else ''}
        {'<input type="hidden" name="to_date" value="' + to_date_str + '">' if to_date_str else ''}
        <input type="text" name="q" placeholder="Search Jira key…" value="{q}">
        <button type="submit">Search</button>
      </form>
    </div>

    <!-- History table -->
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Direction</th>
          <th>Event</th>
          <th>Jira Key</th>
          <th>Lark Record</th>
          <th>Description</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>

    {pagination}
  </div>

</div>

<script>
function toggleCustom(e) {{
  e.preventDefault();
  var f = document.getElementById('custom-form');
  f.style.display = f.style.display === 'none' ? 'flex' : 'none';
}}

async function loadTables() {{
  var picker = document.getElementById('table-picker');
  picker.style.display = 'block';
  picker.innerHTML = '<span style="color:#64748b;font-size:12px">Loading tables…</span>';
  try {{
    var res = await fetch('/api/tables');
    var data = await res.json();
    if (data.error) {{ picker.innerHTML = '<span style="color:#ef4444">Error: ' + data.error + '</span>'; return; }}
    var html = '<div class="table-list">';
    data.tables.forEach(function(t) {{
      var active = t.table_id === data.active_table_id;
      html += '<div class="table-item' + (active ? ' active-tbl' : '') + '">';
      html += '<div><div>' + t.name + (active ? ' ✓' : '') + '</div>';
      html += '<div class="tbl-id">' + t.table_id + '</div></div>';
      html += '<button ' + (active ? 'disabled' : '') + ' onclick="switchTable(\'' + t.table_id + '\',\'' + t.name.replace(/'/g,"\\'") + '\')">Select</button>';
      html += '</div>';
    }});
    html += '</div>';
    picker.innerHTML = html;
  }} catch(e) {{ picker.innerHTML = '<span style="color:#ef4444">Failed to load tables.</span>'; }}
}}

async function switchTable(id, name) {{
  if (!confirm('Switch sync to table "' + name + '"?\\nThis will rebuild the index.')) return;
  var res = await fetch('/settings/table', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{table_id: id, name: name}})
  }});
  var data = await res.json();
  if (data.ok) {{
    document.getElementById('active-table-label').textContent = name + ' (' + id + ')';
    document.getElementById('table-picker').style.display = 'none';
    alert('Switched to "' + name + '". ' + data.linked + ' records indexed.');
  }} else {{
    alert('Error: ' + (data.error || 'unknown'));
  }}
}}
</script>
</body>
</html>"""
    return html


@app.get("/debug/payloads")
async def debug_payloads():
    """Last raw webhook payloads received."""
    return list(_raw_payloads)


@app.get("/debug/index")
async def debug_index():
    """Current in-memory index of linked Jira ↔ Lark records."""
    return {
        "linked_count": len(index._jira_to_lark),
        "jira_to_lark": index._jira_to_lark,
    }


@app.post("/debug/rebuild")
async def debug_rebuild():
    """Force-rebuild the index from all current Lark records."""
    cfg = get_cfg()
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
    index.rebuild(records)
    return {"rebuilt": True, "linked_count": len(index._jira_to_lark)}


@app.post("/webhook/lark")
async def recv_lark(request: Request, bg: BackgroundTasks):
    body = await request.json()
    _raw_payloads.appendleft({"source": "lark", "body": body})
    logging.getLogger(__name__).info(f"Lark webhook received: {json.dumps(body)[:500]}")

    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}

    if not _sync_enabled:
        logging.getLogger(__name__).info("Lark webhook ignored — sync disabled")
        return {"ok": True, "note": "sync disabled"}

    event = body.get("event", {})
    cfg = get_cfg()
    action_list = event.get("action_list", [])
    logging.getLogger(__name__).info(
        f"Lark event: table_id={event.get('table_id')} actions={len(action_list)}"
    )
    for action in action_list:
        bg.add_task(lark_handler.process, action, event.get("table_id"), cfg)
    return {"ok": True}


@app.post("/webhook/lark-auto")
async def recv_lark_auto(request: Request, bg: BackgroundTasks):
    """Receives webhooks from Lark Base Automation (no bot file-access needed).
    Body: {"action": "record_added|record_edited|record_deleted", "record_id": "...", "table_id": "..."}
    """
    body = await request.json()
    _raw_payloads.appendleft({"source": "lark-auto", "body": body})
    logging.getLogger(__name__).info(f"Lark-auto webhook: {body}")

    if not _sync_enabled:
        return {"ok": True, "note": "sync disabled"}

    action = {
        "action":    body.get("action", "record_edited"),
        "record_id": body.get("record_id", ""),
    }
    table_id = body.get("table_id", get_cfg().get("LARK_TABLE_ID", ""))
    cfg = get_cfg()
    bg.add_task(lark_handler.process, action, table_id, cfg)
    return {"ok": True}


@app.post("/webhook/jira")
async def recv_jira(request: Request, bg: BackgroundTasks):
    body = await request.json()
    _raw_payloads.appendleft({"source": "jira", "body": body})
    logging.getLogger(__name__).info(
        f"Jira webhook received: event={body.get('webhookEvent')} key={body.get('issue',{}).get('key')}"
    )

    if not _sync_enabled:
        logging.getLogger(__name__).info("Jira webhook ignored — sync disabled")
        return {"ok": True, "note": "sync disabled"}

    cfg = get_cfg()
    bg.add_task(jira_handler.process,
                body.get("webhookEvent"),
                body.get("issue", {}),
                body.get("changelog", {}),
                cfg)
    return {"ok": True}
