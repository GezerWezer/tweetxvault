"""Focused first-run authentication and X archive setup endpoints."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from tweetxvault.config import (
    AuthConfig,
    ensure_paths,
    load_config,
    resolve_paths,
    update_config_values,
)
from tweetxvault.web.deps import server_state, verify_credentials
from tweetxvault.web.routes.activity import _read_active_snapshot, _start_activity

router = APIRouter(prefix="/api/setup", tags=["setup"])
MASKED_SECRET = "********"
MAX_ARCHIVE_BYTES = 50 * 1024**3


class AuthUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_token: str | None = None
    ct0: str | None = None
    user_id: str | None = None


def _auth_payload(config: AuthConfig) -> dict[str, object]:
    values = config.model_dump()
    for field in ("auth_token", "ct0"):
        if values.get(field):
            values[field] = MASKED_SECRET
    configured = bool(config.auth_token and config.ct0)
    return {"configured": configured, "values": values}


def _candidate_auth(request: AuthUpdateRequest, current: AuthConfig) -> AuthConfig:
    values: dict[str, str | None] = {}
    for field, value in request.model_dump().items():
        if field not in request.model_fields_set:
            value = getattr(current, field)
        if field in {"auth_token", "ct0"} and value == MASKED_SECRET:
            value = getattr(current, field)
        if isinstance(value, str):
            value = value.strip() or None
        values[field] = value
    return AuthConfig.model_validate(values)


def _test_auth_candidate(candidate: AuthConfig) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("TWEETXVAULT_AUTH_TOKEN", "TWEETXVAULT_CT0", "TWEETXVAULT_USER_ID"):
        env.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="tweetxvault-auth-") as config_root:
        env["XDG_CONFIG_HOME"] = config_root
        temporary_paths = ensure_paths(resolve_paths(env))
        update_config_values(
            temporary_paths,
            {
                "auth.auth_token": candidate.auth_token,
                "auth.ct0": candidate.ct0,
                "auth.user_id": candidate.user_id,
            },
        )
        return subprocess.run(
            [sys.executable, "-m", "tweetxvault", "auth", "check"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )


def _archive_payload() -> dict[str, object]:
    paths = server_state.get("paths")
    store = server_state.get("store")
    if paths is None or store is None:
        raise HTTPException(status_code=503, detail="Setup storage is not initialized.")
    archive = paths.staged_archive_file
    uploaded = archive.is_file()
    imported = store.has_completed_archive_import()
    pending = store.count_incomplete_initial_enrichment() if imported else 0
    enriched = imported and pending == 0
    warnings: list[str] = []
    if not imported:
        warnings.append("An X archive has not been imported yet.")
    if not enriched:
        warnings.append(
            "Archive enrichment has not finished yet."
            if imported
            else "Archive enrichment will still be required after import."
        )
    return {
        "uploaded": uploaded,
        "filename": archive.name if uploaded else None,
        "size": archive.stat().st_size if uploaded else None,
        "uploaded_at": archive.stat().st_mtime if uploaded else None,
        "imported": imported,
        "enriched": enriched,
        "pending_enrichment": pending,
        "warnings": warnings,
    }


@router.get("")
def get_setup(_auth: Annotated[bool, Depends(verify_credentials)]) -> dict[str, object]:
    config, _ = load_config()
    return {"auth": _auth_payload(config.auth), "archive": _archive_payload()}


@router.put("/auth")
def put_auth(
    request: AuthUpdateRequest,
    _auth: Annotated[bool, Depends(verify_credentials)],
) -> dict[str, object]:
    paths = server_state.get("paths")
    if paths is None:
        raise HTTPException(status_code=503, detail="Setup storage is not initialized.")
    current, _ = load_config()
    candidate = _candidate_auth(request, current.auth)
    if not candidate.auth_token or not candidate.ct0:
        raise HTTPException(
            status_code=422,
            detail="Authentication failed: auth_token and ct0 are required.",
        )
    try:
        result = _test_auth_candidate(candidate)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail=f"Authentication test failed: {exc}") from exc
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        detail = output[-8000:] or "The X authentication probe did not succeed."
        raise HTTPException(status_code=422, detail=detail)

    changes: dict[str, object] = {}
    for field, value in request.model_dump(exclude_unset=True).items():
        if field in {"auth_token", "ct0"} and value == MASKED_SECRET:
            continue
        changes[f"auth.{field}"] = value.strip() if isinstance(value, str) else value
    try:
        update_config_values(paths, changes)
        config, _ = load_config()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    server_state["config"] = config
    if supervisor := server_state.get("job_supervisor"):
        supervisor.config = config
    return _auth_payload(config.auth)


@router.put("/archive")
async def upload_archive(
    request: Request,
    _auth: Annotated[bool, Depends(verify_credentials)],
) -> dict[str, object]:
    paths = server_state.get("paths")
    if paths is None:
        raise HTTPException(status_code=503, detail="Setup storage is not initialized.")
    if _read_active_snapshot() is not None:
        raise HTTPException(status_code=409, detail="Wait for the active command to finish.")
    archive = paths.staged_archive_file
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_name(f".{archive.name}.{os.getpid()}.upload")
    size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise HTTPException(status_code=413, detail="Archive exceeds the 50 GiB limit.")
                handle.write(chunk)
        if size == 0 or not zipfile.is_zipfile(temporary):
            raise HTTPException(status_code=422, detail="Upload must be a valid archive.zip file.")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return _archive_payload()


@router.delete("/archive")
def clear_archive(_auth: Annotated[bool, Depends(verify_credentials)]) -> dict[str, object]:
    paths = server_state.get("paths")
    if paths is None:
        raise HTTPException(status_code=503, detail="Setup storage is not initialized.")
    if _read_active_snapshot() is not None:
        raise HTTPException(status_code=409, detail="Wait for the active command to finish.")
    paths.staged_archive_file.unlink(missing_ok=True)
    return _archive_payload()


@router.post("/archive/import", status_code=status.HTTP_202_ACCEPTED)
def import_archive(_auth: Annotated[bool, Depends(verify_credentials)]) -> dict[str, object]:
    paths = server_state.get("paths")
    if paths is None or not paths.staged_archive_file.is_file():
        raise HTTPException(status_code=409, detail="Upload archive.zip before importing it.")
    return _start_activity(
        "import",
        ["import", "x-archive", str(paths.staged_archive_file)],
        "tweetxvault import x-archive",
    )
