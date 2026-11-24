"""Web app: serves the page in public/ and the agent's API on http://localhost:PORT (default 3000)."""
import agent.env  # noqa: F401 - loads .env before anything reads the environment

import asyncio
import json
import os
import re
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.capacity import buying_capacity
from agent.db import sync_db
from agent.discover import find_companies
from agent.export import xlsx
from agent.people import MAX_PEOPLE, apply_notes, case_study, growth_plan, enrich_company, mark_outreach, people_providers, review_company, rewrite_message, save_lead
from agent.pipeline import LEADS_DIR, run_agent
from agent.rerank import warmup
from agent.util import to_json

PORT = int(os.environ.get("PORT") or 3000)
PUBLIC = Path(__file__).resolve().parent / "public"
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/styles.css": ("styles.css", "text/css")}


def reply(status, body):
    return Response(to_json(body), status_code=status, media_type="application/json", headers={"Cache-Control": "no-store"})


@asynccontextmanager
async def lifespan(_app):
    synced = await asyncio.to_thread(sync_db, LEADS_DIR)
    print(f"Database leads/leads.db: {synced} runs in sync")
    print(f"Account Fit Agent running at http://localhost:{PORT}", flush=True)
    warmup()  # download/load the reranker in the background
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Results hold people's contact details and runs cost money, so other websites must not reach this server through the browser.
    if not re.match(r"^(localhost|127\.0\.0\.1)(:\d+)?$", request.headers.get("host", "")):
        return reply(403, {"error": "Forbidden"})
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def not_found(_request, _exc):
    return reply(404, {"error": "Not found"})


def asset(path):
    file, kind = ASSETS[path]
    # no-cache: the browser re-checks every load, so it never runs an old app.js after an update
    return Response((PUBLIC / file).read_bytes(), media_type=f"{kind}; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index():
    return asset("/")


@app.get("/app.js")
async def app_js():
    return asset("/app.js")


@app.get("/styles.css")
async def styles():
    return asset("/styles.css")


@app.get("/api/config")
async def config():
    return reply(200, {"product": os.environ.get("PRODUCT_DESCRIPTION", ""), "criteria": os.environ.get("IDEAL_CUSTOMER", ""),
                       "ready": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")), "people": people_providers(), "max_people": MAX_PEOPLE})


@app.get("/api/latest")
async def latest():
    try:
        result = json.loads((LEADS_DIR / "leads.json").read_text(encoding="utf-8"))
        for company in (result or {}).get("companies") or []:  # older results predate the estimate
            company.setdefault("buying_capacity", buying_capacity(company.get("financials"), company.get("willingness_to_buy")))
        return reply(200, result)
    except (OSError, ValueError):
        return reply(200, None)


async def read_json(request, limit=50_000):
    """The request's JSON object, or an error response."""
    if not re.match(r"^application/json\b", request.headers.get("content-type", "")):
        return None, reply(415, {"error": "Send JSON."})
    raw = await request.body()
    if len(raw.decode("utf-8", "replace")) > limit:
        return None, reply(413, {"error": "Too large."})
    try:
        body = json.loads(raw)
    except ValueError:
        return None, reply(400, {"error": "Invalid JSON"})
    return (body if isinstance(body, dict) else {}), None


ACTIONS = {"/api/review": review_company, "/api/people": enrich_company, "/api/save-lead": save_lead, "/api/message": rewrite_message, "/api/outreach": mark_outreach, "/api/notes": apply_notes, "/api/case-study": case_study, "/api/growth": growth_plan, "/api/discover": find_companies}


@app.post("/api/review")
@app.post("/api/people")
@app.post("/api/save-lead")
@app.post("/api/message")
@app.post("/api/outreach")
@app.post("/api/notes")
@app.post("/api/case-study")
@app.post("/api/growth")
@app.post("/api/discover")
async def action(request: Request):
    body, error = await read_json(request)
    if error:
        return error
    try:
        return reply(200, await ACTIONS[request.url.path](body))
    except Exception as e:  # noqa: BLE001 - any problem goes back to the page as a message
        return reply(400, {"error": str(e)})


@app.post("/api/export.xlsx")
async def export_xlsx(request: Request):
    body, error = await read_json(request, 5_000_000)
    if error:
        return error
    try:
        data = xlsx(body.get("sheets"))
    except ValueError as e:
        return reply(400, {"error": str(e)})
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Cache-Control": "no-store"})


@app.post("/api/run")
async def run(request: Request):
    body, error = await read_json(request, 14_000_000)  # room for a 10 MB deck sent as base64
    if error:
        return error
    # NDJSON stream: one event per line so the browser can show progress live.
    queue = asyncio.Queue()

    async def work():
        try:
            result = await run_agent(body, queue.put_nowait)
            queue.put_nowait({"type": "result", "result": result})
        except Exception as e:  # noqa: BLE001 - reported to the page
            traceback.print_exc()
            queue.put_nowait({"type": "error", "message": str(e) or "Run failed."})
        queue.put_nowait(None)

    # The run keeps going and saves its result even if the browser disconnects.
    task = asyncio.create_task(work())

    async def stream():
        while (event := await queue.get()) is not None:
            yield to_json(event) + "\n"
        await task

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
