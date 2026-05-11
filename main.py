"""FastAPI entry point: /health + /webhook/lark + /webhook/jira + reconcile loop."""
import asyncio
import logging
from fastapi import FastAPI, Request, BackgroundTasks
import lark_api, index, reconcile
import lark_handler, jira_handler
from config import get_cfg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI()


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
        await asyncio.sleep(1800)  # 30 min
        await asyncio.to_thread(reconcile.run, get_cfg())


@app.get("/health")
async def health():
    """Pinged by cron-job.org every 10 min to prevent Render free-tier sleep."""
    return {"ok": True}


@app.post("/webhook/lark")
async def recv_lark(request: Request, bg: BackgroundTasks):
    body = await request.json()
    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}
    event = body.get("event", {})
    cfg = get_cfg()
    for action in event.get("action_list", []):
        bg.add_task(lark_handler.process, action, event.get("table_id"), cfg)
    return {"ok": True}


@app.post("/webhook/jira")
async def recv_jira(request: Request, bg: BackgroundTasks):
    body = await request.json()
    cfg = get_cfg()
    bg.add_task(jira_handler.process,
                body.get("webhookEvent"),
                body.get("issue", {}),
                body.get("changelog", {}),
                cfg)
    return {"ok": True}
