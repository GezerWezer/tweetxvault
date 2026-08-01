"""FastAPI web server for tweetxvault."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from rich.console import Console

from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.reminders import (
    print_archive_migration_report,
    print_pending_archive_enrichment_reminder,
)
from tweetxvault.storage import open_archive_store
from tweetxvault.web.deps import server_state, verify_credentials
from tweetxvault.web.routes.avatars import router as avatars_router
from tweetxvault.web.routes.config import router as config_router
from tweetxvault.web.routes.stats import router as stats_router
from tweetxvault.web.routes.storage_stats import router as storage_stats_router
from tweetxvault.web.routes.tags import router as tags_router
from tweetxvault.web.routes.tweets import router as tweets_router


def _build_fts_in_background(store) -> None:
    """Build the FTS index in a daemon thread so the server can start immediately."""
    try:
        store.ensure_fts_index()
    except Exception:
        pass  # FTS is optional; search degrades gracefully without it


@asynccontextmanager
async def lifespan(app: FastAPI):
    if "paths" in server_state:
        store = open_archive_store(
            server_state["paths"],
            create=False,
            config=server_state.get("config"),
        )
        if store is None:
            raise RuntimeError("Archive database not found")
        server_state["store"] = store
        console = Console(stderr=True)
        print_archive_migration_report(console, store)
        print_pending_archive_enrichment_reminder(console, store)
        store.ensure_scalar_indexes()
        t = threading.Thread(
            target=_build_fts_in_background,
            args=(store,),
            daemon=True,
        )
        t.start()
    try:
        yield
    finally:
        if store := server_state.get("store"):
            store.close()


app = FastAPI(title="tweetxvault Web UI", lifespan=lifespan)

# Include route modules
app.include_router(tweets_router)
app.include_router(tags_router)
app.include_router(config_router)
app.include_router(stats_router)
app.include_router(avatars_router)
app.include_router(storage_stats_router)

# Mount static assets directory if available
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
def read_root(_auth: Annotated[bool, Depends(verify_credentials)]):
    html_path = Path(__file__).parent / "index.html"
    return FileResponse(html_path)


@app.get("/media/{media_path:path}", response_class=FileResponse)
def read_media(
    media_path: str,
    _auth: Annotated[bool, Depends(verify_credentials)],
):
    """Serve archive media only after auth and never follow paths outside media_dir."""
    paths = server_state.get("paths")
    if paths is None:
        raise HTTPException(status_code=404, detail="Media file not found")

    media_dir = paths.media_dir.resolve()
    target = (media_dir / media_path).resolve()
    if not target.is_relative_to(media_dir) or not target.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return FileResponse(target)


def run_server(
    config: AppConfig | None,
    paths: XDGPaths,
    host: str,
    port: int,
    password_hash: str | None,
) -> None:
    import uvicorn

    server_state["config"] = config
    server_state["paths"] = paths
    server_state["password_hash"] = password_hash
    uvicorn.run(app, host=host, port=port, log_level="info")
