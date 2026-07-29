"""FastAPI web server for tweetxvault."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.storage import open_archive_store
from tweetxvault.web.deps import server_state, verify_credentials
from tweetxvault.web.routes.tweets import router as tweets_router
from tweetxvault.web.routes.tags import router as tags_router
from tweetxvault.web.routes.config import router as config_router
from tweetxvault.web.routes.stats import router as stats_router
from tweetxvault.web.routes.avatars import router as avatars_router
from tweetxvault.web.routes.storage_stats import router as storage_stats_router

def _build_fts_in_background(store) -> None:
    """Build the FTS index in a daemon thread so the server can start immediately."""
    try:
        store.ensure_fts_index()
    except Exception:
        pass  # FTS is optional; search degrades gracefully without it

@asynccontextmanager
async def lifespan(app: FastAPI):
    if "paths" in server_state:
        server_state["store"] = open_archive_store(server_state["paths"], create=False, config=server_state.get("config"))
        server_state["store"].ensure_scalar_indexes()
        t = threading.Thread(
            target=_build_fts_in_background,
            args=(server_state["store"],),
            daemon=True,
        )
        t.start()
    yield
    if "store" in server_state and server_state["store"]:
        server_state["store"].close()

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
def read_root(_auth: bool = Depends(verify_credentials)):
    html_path = Path(__file__).parent / "index.html"
    return FileResponse(html_path)

def run_server(config: AppConfig | None, paths: XDGPaths, host: str, port: int, password_hash: str | None) -> None:
    import uvicorn
    server_state["config"] = config
    server_state["paths"] = paths
    server_state["password_hash"] = password_hash
    app.mount("/media", StaticFiles(directory=paths.media_dir), name="media")
    uvicorn.run(app, host=host, port=port, log_level="info")