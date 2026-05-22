"""
main.py — FastAPI application entry point for Denali Health BD Automation.

Run locally:
    uvicorn main:app --reload --host 127.0.0.1 --port 8000

Or simply:
    python main.py

Open http://127.0.0.1:8000        → static frontend (when built)
Open http://127.0.0.1:8000/docs   → interactive API docs
Open http://127.0.0.1:8000/health → health check
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from database import init_db
from routers import campaigns, contacts, drafts, leads, opportunities, sync
from services.auth import BasicAuthMiddleware
from services import scheduler as sheets_scheduler

# ── Config ──────────────────────────────────────
load_dotenv()
APP_ENV  = os.getenv("APP_ENV", "development")
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Comma-separated list of origins allowed for CORS (production lock-down).
# Empty / unset = wildcard in dev; required in production.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("denali")


# ── Lifespan: runs once on startup, once on shutdown ─────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Denali BD Automation API (env=%s)", APP_ENV)
    init_db()
    log.info("Database initialised")
    sheets_scheduler.start()
    yield
    await sheets_scheduler.stop()
    log.info("Shutting down")


# ── App ─────────────────────────────────────────
app = FastAPI(
    title="Denali Health — BD Automation",
    description=(
        "Semi-automated outbound BD for clinical-trial site selection. "
        "Identifies trial opportunities, enriches contacts, scores them, "
        "drafts personalised emails — every send requires human approval."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Basic auth — enforced whenever APP_PASSWORD is set (no-op otherwise)
app.add_middleware(BasicAuthMiddleware)

# CORS — wide open in dev, locked to ALLOWED_ORIGINS in prod
if APP_ENV == "development":
    cors_origins = ["*"]
else:
    cors_origins = ALLOWED_ORIGINS or []   # explicit empty = block all cross-origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API routers ─────────────────────────────────
app.include_router(opportunities.router, prefix="/api/opportunities", tags=["opportunities"])
app.include_router(contacts.router,      prefix="/api/contacts",      tags=["contacts"])
app.include_router(drafts.router,        prefix="/api/drafts",        tags=["drafts"])
app.include_router(campaigns.router,     prefix="/api/campaigns",     tags=["campaigns"])
app.include_router(sync.router,          prefix="/api/sync",          tags=["sync"])
app.include_router(leads.router,         prefix="/api/leads",         tags=["leads"])


# ── Health check ────────────────────────────────
@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "version": app.version, "env": APP_ENV}


# ── Static frontend ─────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    

    @app.get("/", include_in_schema=False)
    def index():
        index_html = STATIC_DIR / "index.html"
        if index_html.exists() and index_html.stat().st_size > 0:
            return FileResponse(index_html)
        return {
            "message": "API is running. Frontend not yet built.",
            "docs": "/docs",
            "health": "/health",
        }


# ── `python main.py` entry point ────────────────


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=(APP_ENV == "development"))
