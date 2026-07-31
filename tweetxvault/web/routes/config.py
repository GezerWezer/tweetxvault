"""Configuration management endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tweetxvault.web.deps import verify_credentials

router = APIRouter()
MASKED_SECRET = "********"
SECRET_FIELDS = (
    ("auth", "auth_token"),
    ("auth", "ct0"),
    ("web", "password_hash"),
    ("tagging", "api_key"),
)


class ConfigUpdateRequest(BaseModel):
    auth: dict
    sync: dict
    web: dict
    database: dict
    tagging: dict


def _mask_secrets(data: dict) -> dict:
    for section, field in SECRET_FIELDS:
        section_data = data.get(section)
        if isinstance(section_data, dict) and section_data.get(field):
            section_data[field] = MASKED_SECRET
    return data


def _restore_masked_secrets(data: dict, current_config) -> dict:
    for section, field in SECRET_FIELDS:
        section_data = data.get(section)
        if not isinstance(section_data, dict) or section_data.get(field) != MASKED_SECRET:
            continue
        section_data[field] = getattr(getattr(current_config, section), field)
    return data


@router.get("/api/config")
def api_get_config(_auth: Annotated[bool, Depends(verify_credentials)]):
    try:
        from tweetxvault.config import load_config

        config, _ = load_config()
        return _mask_secrets(config.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/config/defaults")
def api_get_config_defaults(_auth: Annotated[bool, Depends(verify_credentials)]):
    try:
        from tweetxvault.config import AppConfig

        return AppConfig().model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/config/schema")
def api_get_config_schema(_auth: Annotated[bool, Depends(verify_credentials)]):
    try:
        from tweetxvault.config import get_config_ui_schema

        return get_config_ui_schema()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/config")
def api_post_config(
    req: ConfigUpdateRequest,
    _auth: Annotated[bool, Depends(verify_credentials)],
):
    try:
        from tweetxvault.config import AppConfig, load_config, save_app_config

        config, paths = load_config()
        req_data = _restore_masked_secrets(req.model_dump(), config)

        new_config = AppConfig.model_validate(req_data)
        save_app_config(paths, new_config)
        return {"status": "ok"}
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e)) from e
