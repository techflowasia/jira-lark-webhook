"""FastAPI entry point: / dashboard + /health + /webhook/lark + /webhook/jira + reconcile loop."""
import asyncio
import html as _html
import json
import re
import logging
import urllib.request
import urllib.parse
from collections import deque
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
import lark_api, jira_api, index, reconcile, history, field_mappings
import lark_handler, jira_handler, projects, projects_api, routing, dashboard, wizard
import config as _cfg_mod
from config import get_cfg, set_active_table

REDIRECT_URI = f"{_cfg_mod.PUBLIC_URL}/auth/callback"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI()

# F_PARENT writes don't round-trip with get_record's read-shape — update_record
# invalidates the cache entry instead of merging a wrong-shape parent value
# (which would trigger redundant parent re-writes on next update). Checked
# dynamically in lark_api.update_record() via field_mappings.F_PARENT, so a
# dashboard rename of the parent field is still caught with no restart needed.

# Store last 20 raw payloads for debugging
_raw_payloads: deque = deque(maxlen=20)


def _load_toggle_states() -> None:
    """Only the global Lark value-cache flag is global; sync/reconcile toggles
    live on each Project."""
    try:
        client = history._get_client()
        if not client:
            return
        rows = client.table("settings").select("key,value") \
            .in_("key", ["lark_value_cache_enabled"]).execute()
        for r in (rows.data or []):
            if r["key"] == "lark_value_cache_enabled":
                lark_api.set_value_cache_enabled(r["value"].lower() != "false")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not load toggle states: {e}")


@app.on_event("startup")
async def startup() -> None:
    log = logging.getLogger(__name__)
    # Project registry must be ready before any loader or get_cfg() call.
    try:
        projects.load_all()
        projects.bootstrap_default()
    except Exception as e:
        log.error(f"Project registry bootstrap failed (using env project): {e}")
    # Build the in-memory index, but never let a Lark outage crash startup —
    # if the bind fails, Render's port scan kills the container and we never
    # come back. Empty index means new webhooks may arrive unlinked, but the
    # 30-min reconcile loop and the auto-discover path in _handle_update will
    # repair the link as soon as Lark recovers.
    asyncio.create_task(_initial_index_rebuild())
    asyncio.create_task(_reconcile_loop())
    asyncio.create_task(_keepalive_loop())
    asyncio.create_task(asyncio.to_thread(field_mappings.load))
    asyncio.create_task(asyncio.to_thread(_load_toggle_states))


async def _initial_index_rebuild() -> None:
    """Build every project's index sequentially; one project's failure never
    blocks the others."""
    log = logging.getLogger(__name__)
    for p in projects.all_projects():
        try:
            cfg = projects.cfg_for(p)
            token = await asyncio.to_thread(
                lark_api.get_token, cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
            records = await asyncio.to_thread(
                lark_api.fetch_all_records, token, p.lark_base_token, p.lark_table_id)
            with projects.use(p.id):
                index.rebuild(records)
                log.info(f"Index built for project {p.id} ({p.jira_project_key}): "
                         f"{len(index._jira_to_lark)} linked records")
        except Exception as e:
            log.error(f"Initial index rebuild failed for project {p.id} "
                      f"(continuing with empty index): {e}")


async def _keepalive_loop() -> None:
    """Ping own /health every 4.5 min to prevent Render free-tier spindown (15 min threshold).
    Must hit the external URL so the request passes through Render's load balancer;
    localhost pings are invisible to Render and don't reset the spindown timer."""
    import os as _os
    external = _os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external:
        url = f"{external}/health"
    else:
        port = int(_os.environ.get("PORT", 10000))
        url = f"http://localhost:{port}/health"
    log = logging.getLogger(__name__)
    log.info(f"Keepalive targeting: {url}")
    while True:
        await asyncio.sleep(270)  # 4.5 min — safely under 15 min spindown threshold
        try:
            urllib.request.urlopen(url, timeout=10)
            log.debug("Keepalive ping OK")
        except Exception as e:
            log.warning(f"Keepalive ping failed: {e}")


def _reconcile_all() -> None:
    """One reconcile pass over every active, reconcile-enabled project.
    A failure in one project never stops the others."""
    log = logging.getLogger(__name__)
    for p in projects.all_projects():
        if not (p.reconcile_enabled and p.setup_state == "active"):
            log.info(f"Reconcile skipped for project {p.id} — disabled or not active")
            continue
        try:
            reconcile.run(projects.cfg_for(p))
        except Exception as e:
            log.error(f"Reconcile failed for project {p.id} ({p.jira_project_key}): {e}")


async def _reconcile_loop() -> None:
    while True:
        # 6 h, not 30 min: reconcile is a safety net for missed webhooks, not the
        # primary sync path. At 30 min it alone burned ~21k Lark calls/month and
        # blew the tenant API quota. The dashboard "Run Backfill" button still
        # forces an immediate full reconcile when needed.
        await asyncio.sleep(21600)
        await asyncio.to_thread(_reconcile_all)


@app.get("/auth/start")
async def auth_start(project: str = ""):
    """Step 1 — redirect user to Lark OAuth to get a user access token.
    `?project=N` carries the project through OAuth `state` so the callback
    connects that project's Base (no param -> legacy state, project #1)."""
    state = "add_bot"
    if project != "":
        if not re.fullmatch(r"[1-9][0-9]*", project):
            raise HTTPException(400, "project must be a positive number")
        pid = int(project)
        if projects.get(pid) is None:
            raise HTTPException(404, f"project {pid} not found")
        state = f"project:{pid}"
    params = urllib.parse.urlencode({
        "app_id":       get_cfg()["LARK_APP_ID"],
        "redirect_uri": REDIRECT_URI,
        "scope":        "drive:drive",
        "state":        state,
    })
    return RedirectResponse(f"{lark_api.LARK_BASE_URL}/authen/v1/authorize?{params}")


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback(request: Request):
    """Step 2 — exchange code for user token, add bot as editor on the Base."""
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h2>Error: no code in callback</h2>", status_code=400)

    # state: "project:<id>" (wizard) or the legacy "add_bot" (project #1).
    state = request.query_params.get("state", "")
    if state == "add_bot":
        pid = 1
    elif re.fullmatch(r"project:[1-9][0-9]*", state):
        pid = int(state.split(":", 1)[1])
    else:
        return HTMLResponse("<h2>Error: invalid state</h2>", status_code=400)
    proj = projects.get(pid)
    if proj is None:
        return HTMLResponse(f"<h2>Error: project {_html.escape(str(pid))} not found</h2>", status_code=400)
    cfg = projects.cfg_for(proj)
    # Step 1 — get app_access_token
    r0 = urllib.request.Request(
        f"{lark_api.LARK_BASE_URL}/auth/v3/app_access_token/internal",
        data=json.dumps({"app_id": cfg["LARK_APP_ID"], "app_secret": cfg["LARK_APP_SECRET"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    app_token = json.loads(urllib.request.urlopen(r0).read())["app_access_token"]

    # Step 2 — exchange auth code for user access token
    req = urllib.request.Request(
        f"{lark_api.LARK_BASE_URL}/authen/v1/access_token",
        data=json.dumps({"grant_type": "authorization_code", "code": code}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {app_token}"},
        method="POST",
    )
    token_resp = json.loads(urllib.request.urlopen(req).read())
    user_token = token_resp.get("data", {}).get("access_token")
    if not user_token:
        return HTMLResponse(f"<h2>Token exchange failed</h2><pre>{_html.escape(json.dumps(token_resp, indent=2))}</pre>", status_code=400)

    # Add bot as editor on the Base using the user's token
    req2 = urllib.request.Request(
        f"{lark_api.LARK_BASE_URL}/drive/v1/permissions/{urllib.parse.quote(cfg['LARK_BASE_TOKEN'], safe='')}/members?type=bitable",
        data=json.dumps({
            "member_type": "openid",
            "member_id":   _cfg_mod.LARK_BOT_OPEN_ID,
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
            f"{lark_api.LARK_BASE_URL}/drive/v1/files/{urllib.parse.quote(cfg['LARK_BASE_TOKEN'], safe='')}/subscribe?file_type=bitable",
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
    <h2 style="color:{color}">{"✅ Lark connected" if ok else "⚠️ Partial setup"}</h2>
    <p>{"✅" if perm_ok else "❌"} {_html.escape(perm_msg)}</p>
    <p>{"✅" if sub_ok else "❌"} {_html.escape(sub_msg)}</p>
    <p>{"Lark → Jira events are now subscribed for this Base." if ok else "Check errors above and try again."}</p>
    <p><a href="/p/{pid}/setup?step=4">Continue project setup →</a> &nbsp; <a href="/p/{pid}">Project page</a></p>
    </body></html>
    """)


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode()).decode()


@app.get("/health")
async def health():
    return {"ok": True,
            "projects": [{"id": p.id, "sync_enabled": p.sync_enabled}
                         for p in projects.all_projects()]}


# Legacy unscoped endpoints: thin aliases for project #1 (the dashboard JS
# still calls them). They share the project-scoped functions in projects_api.
app.include_router(projects_api.router)
_P1 = 1


@app.get("/api/tables")
async def api_tables():
    return await asyncio.to_thread(projects_api.do_tables, _P1)


@app.post("/settings/table")
async def set_table(request: Request):
    """Switch project #1's Lark table; index rebuild runs in background."""
    body = await request.json()
    try:
        return await asyncio.to_thread(
            projects_api.do_set_table, _P1, body.get("table_id", ""), body.get("name", ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/fields")
async def api_fields():
    return await asyncio.to_thread(projects_api.do_fields, _P1)


@app.get("/api/lark-fields")
async def api_lark_fields():
    return await asyncio.to_thread(projects_api.do_lark_fields, _P1)


@app.get("/api/jira-fields")
async def api_jira_fields():
    return await asyncio.to_thread(projects_api.do_jira_fields, _P1)


@app.post("/settings/fields")
async def save_field(request: Request):
    return await asyncio.to_thread(projects_api.do_save_field, _P1, await request.json())


@app.delete("/settings/fields/{mapping_id}")
async def delete_field(mapping_id: int):
    return await asyncio.to_thread(projects_api.do_delete_field, _P1, mapping_id)


@app.get("/api/jira-issue-types")
async def api_jira_issue_types():
    return await asyncio.to_thread(projects_api.do_jira_issue_types, _P1)


@app.get("/api/lark-type-options")
async def api_lark_type_options():
    return await asyncio.to_thread(projects_api.do_lark_type_options, _P1)


@app.get("/api/sync-types")
async def api_sync_types():
    return await asyncio.to_thread(projects_api.do_sync_types, _P1)


@app.post("/settings/sync-types")
async def save_sync_types_endpoint(request: Request):
    return await asyncio.to_thread(projects_api.do_save_sync_types, _P1, await request.json())


@app.post("/api/backfill")
async def api_backfill():
    return await asyncio.to_thread(projects_api.do_backfill, _P1)


def _flip(pid: int, field: str, label: str) -> None:
    p = projects.get(pid)
    if p is None:
        return
    new = not getattr(p, field)
    projects.update(pid, {field: new})
    state = "enabled" if new else "disabled"
    logging.getLogger(__name__).info(f"{label} {state} for project {pid} via dashboard toggle")
    with projects.use(pid):
        history.record(direction="system", event="config",
                       description=f"{label} {state} via dashboard")


@app.post("/p/{pid}/toggle")
async def toggle_sync_project(pid: int):
    _flip(pid, "sync_enabled", "Sync")
    return RedirectResponse(f"/p/{pid}", status_code=303)


@app.post("/p/{pid}/toggle/reconcile")
async def toggle_reconcile_project(pid: int):
    _flip(pid, "reconcile_enabled", "Reconcile")
    return RedirectResponse(f"/p/{pid}", status_code=303)


@app.post("/toggle")
async def toggle_sync():
    """Legacy route: acts on project #1."""
    _flip(1, "sync_enabled", "Sync")
    return RedirectResponse("/", status_code=303)


@app.post("/toggle/reconcile")
async def toggle_reconcile():
    """Legacy route: acts on project #1."""
    _flip(1, "reconcile_enabled", "Reconcile")
    return RedirectResponse("/", status_code=303)


@app.post("/toggle/lark-value-cache")
async def toggle_lark_value_cache():
    """Layer-1 rollback for the Lark record-value cache (no redeploy needed)."""
    new_state = not lark_api._value_cache_enabled
    lark_api.set_value_cache_enabled(new_state)
    state = "enabled" if new_state else "disabled"
    logging.getLogger(__name__).info(f"Lark value cache {state} via dashboard toggle")
    history.record(direction="system", event="config",
                   description=f"Lark value cache {state} via dashboard")
    client = history._get_client()
    if client:
        try:
            client.table("settings").upsert(
                {"key": "lark_value_cache_enabled", "value": str(new_state).lower()}).execute()
        except Exception as e:
            logging.getLogger(__name__).warning(f"Could not persist lark_value_cache_enabled: {e}")
    return RedirectResponse("/", status_code=303)


@app.get("/")
async def dashboard_root(request: Request):
    """Redirect to the last viewed project (cookie lp) or the first one."""
    ids = [p.id for p in projects.all_projects()]
    target = ids[0] if ids else 1
    try:
        lp = int(request.cookies.get("lp", ""))
        if lp in ids:
            target = lp
    except ValueError:
        pass
    return RedirectResponse(f"/p/{target}", status_code=303)


# Registered BEFORE /p/{pid} so they are never shadowed.
@app.get("/p/new", response_class=HTMLResponse)
async def new_project_page():
    return HTMLResponse(await asyncio.to_thread(wizard.render_new))


@app.get("/p/{pid}/setup", response_class=HTMLResponse)
async def project_setup_page(pid: int, step: str = "3"):
    p = projects.get(pid)
    if p is None:
        raise HTTPException(404, f"project {pid} not found")
    return HTMLResponse(await asyncio.to_thread(wizard.render_setup, p, step))


@app.get("/p/{pid}", response_class=HTMLResponse)
async def project_dashboard(pid: int, request: Request):
    p = projects.get(pid)
    if p is None:
        raise HTTPException(404, f"project {pid} not found")
    result = await asyncio.to_thread(dashboard.fetch_history, request, pid)
    resp = HTMLResponse(await asyncio.to_thread(dashboard.render_project, p, request, result))
    resp.set_cookie("lp", str(pid), max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@app.get("/activity", response_class=HTMLResponse)
async def activity(request: Request):
    try:
        pid = int(request.query_params.get("project", ""))
    except ValueError:
        pid = None
    result = await asyncio.to_thread(dashboard.fetch_history, request, pid)
    return HTMLResponse(await asyncio.to_thread(dashboard.render_activity, request, result))


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


@app.get("/debug/lark-calls")
async def debug_lark_calls():
    """Lark API call counts (this process). Watch this to verify the
    quota-reduction work. Authoritative monthly figure is the Lark console."""
    return lark_api.call_stats()


@app.post("/debug/rebuild")
async def debug_rebuild():
    """Force-rebuild the index from all current Lark records."""
    cfg = get_cfg()
    token = lark_api.get_token(cfg["LARK_APP_ID"], cfg["LARK_APP_SECRET"])
    records = lark_api.fetch_all_records(token, cfg["LARK_BASE_TOKEN"], cfg["LARK_TABLE_ID"])
    index.rebuild(records)
    return {"rebuilt": True, "linked_count": len(index._jira_to_lark)}


def _capture(source: str, body, p) -> None:
    _raw_payloads.appendleft({"source": source, "body": body,
                              "routed": p.id if p is not None else None})


def _not_live(source: str, body, p) -> dict:
    logging.getLogger(__name__).info(f"{source} webhook ignored — no live project")
    _raw_payloads.appendleft({"source": source, "body": body, "routed": None})
    return {"ok": True, "note": "no live project"}


@app.post("/webhook/lark")
async def recv_lark(request: Request, bg: BackgroundTasks):
    body = await request.json()
    logging.getLogger(__name__).info(f"Lark webhook received: {json.dumps(body)[:500]}")

    if body.get("type") == "url_verification":
        _raw_payloads.appendleft({"source": "lark", "body": body, "routed": None})
        return {"challenge": body["challenge"]}

    event = body.get("event", {})
    p = routing.route_lark_event(event)
    if not routing.is_live(p):
        return _not_live("lark", body, p)
    _capture("lark", body, p)

    cfg = projects.cfg_for(p)
    action_list = event.get("action_list", [])
    logging.getLogger(__name__).info(
        f"Lark event: project={p.id} table_id={event.get('table_id')} actions={len(action_list)}"
    )
    for action in action_list:
        bg.add_task(lark_handler.process, action, event.get("table_id"), cfg)
    return {"ok": True}


async def _recv_lark_auto(request: Request, bg: BackgroundTasks, project_id):
    body = await request.json()
    logging.getLogger(__name__).info(f"Lark-auto webhook (project {project_id}): {body}")
    p = routing.route_lark_auto(body, project_id)
    if not routing.is_live(p):
        return _not_live("lark-auto", body, p)
    _capture("lark-auto", body, p)

    action = {
        "action":    body.get("action", "record_edited"),
        "record_id": body.get("record_id", ""),
    }
    bg.add_task(lark_handler.process, action, p.lark_table_id, projects.cfg_for(p))
    return {"ok": True}


@app.post("/webhook/lark-auto")
async def recv_lark_auto(request: Request, bg: BackgroundTasks):
    """Receives webhooks from Lark Base Automation (no bot file-access needed).
    Legacy URL: routes to project #1 (or by table_id within project #1's Base).
    Body: {"action": "record_added|record_edited|record_deleted", "record_id": "...", "table_id": "..."}
    """
    return await _recv_lark_auto(request, bg, None)


@app.post("/webhook/lark-auto/{project_id}")
async def recv_lark_auto_project(project_id: int, request: Request, bg: BackgroundTasks):
    return await _recv_lark_auto(request, bg, project_id)


@app.post("/webhook/jira")
async def recv_jira(request: Request, bg: BackgroundTasks):
    body = await request.json()
    logging.getLogger(__name__).info(
        f"Jira webhook received: event={body.get('webhookEvent')} key={body.get('issue',{}).get('key')}"
    )
    p = routing.route_jira(body)
    if not routing.is_live(p):
        return _not_live("jira", body, p)
    _capture("jira", body, p)

    bg.add_task(jira_handler.process,
                body.get("webhookEvent"),
                body.get("issue", {}),
                body.get("changelog", {}),
                projects.cfg_for(p))
    return {"ok": True}
