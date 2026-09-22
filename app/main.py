"""Application entrypoint — one process, one async worker.

The FastAPI app owns a single asynchronous background worker that claims
durable jobs from SQLite. Static files from the frontend production build
are served at ``/``; the API lives under ``/api`` and ``/healthz``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import load_settings
from .routes.dashboard import router as dashboard_router
from .services.worker import Worker

log = logging.getLogger("repairdesk")

STATIC_DIR = Path(os.environ.get("STATIC_DIR", "frontend/dist"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()  # fail-closed: raises on invalid config
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    app.state.settings = settings
    app.state.conn = conn

    worker = Worker(settings)
    task = asyncio.create_task(worker.run(), name="repairdesk-worker")
    app.state.worker = worker
    try:
        yield
    finally:
        worker.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        conn.close()


def create_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(title="Devin Repair Desk", lifespan=lifespan)
    app.include_router(dashboard_router)

    if STATIC_DIR.is_dir():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount(
                "/assets", StaticFiles(directory=assets), name="assets"
            )

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # API routes already matched above; anything else is the SPA.
            candidate = STATIC_DIR / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
