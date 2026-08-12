"""FastAPI web server for tweetxvault."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from rich.console import Console

from tweetxvault.activity_history import cleanup_runs
from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.job_supervisor import JobSupervisor
from tweetxvault.reminders import (
    print_archive_migration_report,
    print_pending_archive_enrichment_reminder,
)
from tweetxvault.scheduler import ScheduleManager
from tweetxvault.storage import open_archive_store
from tweetxvault.web.deps import server_state, verify_credentials
from tweetxvault.web.routes.activity import router as activity_router
from tweetxvault.web.routes.avatars import router as avatars_router
from tweetxvault.web.routes.config import router as config_router
from tweetxvault.web.routes.setup import router as setup_router
from tweetxvault.web.routes.stats import router as stats_router
from tweetxvault.web.routes.storage_stats import router as storage_stats_router
from tweetxvault.web.routes.tags import router as tags_router
from tweetxvault.web.routes.tweets import router as tweets_router
from tweetxvault.web.stats_cache import web_stats_cache


def _build_fts_in_background(store) -> None:
    """Build the FTS index in a daemon thread so the server can start immediately."""
    try:
        store.ensure_fts_index()
    except Exception:
        pass  # FTS is optional; search degrades gracefully without it


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = None
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
        paths = server_state["paths"]
        config = server_state.get("config") or AppConfig()
        cleanup_runs(
            paths.data_dir,
            max_runs=config.activity.max_runs,
            retention_days=config.activity.retention_days,
        )

        def on_job_start(_run_id: str, _pid: int) -> None:
            import time

            server_state["activity_worker_started_at"] = time.time()

        def on_job_complete(_run_id: str, _exit_code: int) -> None:
            server_state.pop("activity_worker_started_at", None)
            web_stats_cache.clear()

        supervisor = JobSupervisor(
            paths,
            config,
            on_start=on_job_start,
            on_complete=on_job_complete,
        )
        scheduler = ScheduleManager(paths, config, supervisor)
        server_state["job_supervisor"] = supervisor
        server_state["schedule_manager"] = scheduler
        scheduler.start()
    try:
        yield
    finally:
        if scheduler := server_state.pop("schedule_manager", None):
            scheduler.stop()
        if supervisor := server_state.pop("job_supervisor", None):
            supervisor.shutdown()
        if store := server_state.get("store"):
            web_stats_cache.wait_for_refreshes()
            web_stats_cache.clear()
            store.close()


app = FastAPI(title="tweetxvault Web UI", lifespan=lifespan)

# Include route modules
app.include_router(tweets_router)
app.include_router(tags_router)
app.include_router(config_router)
app.include_router(stats_router)
app.include_router(avatars_router)
app.include_router(storage_stats_router)
app.include_router(activity_router)
app.include_router(setup_router)

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
