"""Configuration management endpoints."""

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from tweetxvault.web.deps import verify_credentials

router = APIRouter()

class ConfigUpdateRequest(BaseModel):
    auth: dict
    sync: dict
    web: dict
    database: dict
    tagging: dict

@router.get("/api/config")
def api_get_config(_auth: bool = Depends(verify_credentials)):
    try:
        from tweetxvault.config import load_config
        config, _ = load_config()
        data = config.model_dump()
        if data.get("web") and "password_hash" in data["web"]:
            data["web"]["password_hash"] = "********"
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/config/defaults")
def api_get_config_defaults(_auth: bool = Depends(verify_credentials)):
    try:
        from tweetxvault.config import AppConfig
        return AppConfig().model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/config/schema")
def api_get_config_schema(_auth: bool = Depends(verify_credentials)):
    try:
        from tweetxvault.config import get_config_ui_schema
        return get_config_ui_schema()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/config")
def api_post_config(
    req: ConfigUpdateRequest,
    _auth: bool = Depends(verify_credentials)
):
    try:
        from tweetxvault.config import load_config, save_app_config, AppConfig
        config, paths = load_config()
        req_data = req.model_dump()
        
        if req_data.get("web") and req_data["web"].get("password_hash") == "********":
            req_data["web"]["password_hash"] = config.web.password_hash

        new_config = AppConfig.model_validate(req_data)
        save_app_config(paths, new_config)
        return {"status": "ok"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))
