"""FastAPI entry point: / dashboard + /health + /webhook/lark + /webhook/jira + reconcile loop."""
import asyncio
import json
import logging
import urllib.request
import urllib.parse
from collections import deque
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
import lark_api, index, reconcile, history
import lark_handler, jira_handler
from config import get_cfg

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
async def dashboard():
    cfg = get_cfg()
    linked = len(index._jira_to_lark)
    logs = history.recent(200)

    def row_class(status):
        if status == "error":   return "error"
        if status == "skipped": return "skip"
        if status == "system":  return "sys"
        return ""

    def direction_badge(d):
        if d == "system":
            return '<span class="badge sys">System</span>'
        if "lark" in d.split("→")[0]:
            return '<span class="badge lark">Lark → Jira</span>'
        return '<span class="badge jira">Jira → Lark</span>'

    def event_badge(e):
        colors = {"created": "#22c55e", "updated": "#3b82f6",
                  "deleted": "#ef4444", "config": "#a855f7"}
        c = colors.get(e, "#888")
        return f'<span class="evbadge" style="background:{c}">{e}</span>'

    rows = ""
    for entry in logs:
        rc = row_class(entry["status"] if entry["direction"] != "system" else "system")
        err = f'<div class="errmsg">{entry["error"]}</div>' if entry["error"] else ""
        rows += f"""
        <tr class="{rc}">
          <td class="ts">{entry["ts"]}</td>
          <td>{direction_badge(entry["direction"])}</td>
          <td>{event_badge(entry["event"])}</td>
          <td><code>{entry["jira_key"] or "—"}</code></td>
          <td><code class="small">{entry["lark_id"] or "—"}</code></td>
          <td>{entry["description"]}{err}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="6" class="empty">No events recorded yet.</td></tr>'

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
      <div class="label">Events Logged</div>
      <div class="value">{len(logs)}</div>
      <div class="sub">Since last restart</div>
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
      <tr><td class="cfg-key">Lark Table ID</td>
          <td class="cfg-val">{cfg["LARK_TABLE_ID"]}</td></tr>
      <tr><td class="cfg-key">Jira Webhook URL</td>
          <td class="cfg-val">https://jira-lark-webhook.onrender.com/webhook/jira</td></tr>
      <tr><td class="cfg-key">Lark Webhook URL</td>
          <td class="cfg-val">https://jira-lark-webhook.onrender.com/webhook/lark</td></tr>
    </table>
  </div>

  <div class="section">
    <div class="section-header">Sync History (last {len(logs)} events)</div>
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
      <tbody>{rows}</tbody>
    </table>
  </div>

</div>
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
