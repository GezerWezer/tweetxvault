"""Tag management endpoints."""

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException

from tweetxvault.web.deps import get_store, verify_credentials

router = APIRouter()

class TagUpdateRequest(BaseModel):
    tags: list[str]

class TagMergeRequest(BaseModel):
    primary_tag: str
    merge_tags: list[str]

@router.delete("/api/tags/{tweet_id}")
def api_delete_tag(
    tweet_id: str,
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    try:
        store.delete_media_tag(tweet_id)
        return {"success": True}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/tags/{tweet_id}")
def api_update_tag(
    tweet_id: str,
    req: TagUpdateRequest,
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    try:
        store.update_media_tags(tweet_id, req.tags)
        return {"success": True}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/tags/global/{tag}")
def api_delete_global_tag(
    tag: str,
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    try:
        store.delete_global_tag(tag)
        return {"success": True}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/tags/merge")
def api_merge_tags(
    req: TagMergeRequest,
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    try:
        store.merge_global_tags(req.primary_tag, req.merge_tags)
        return {"success": True}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/tags/autocomplete")
def api_tags_autocomplete(
    q: str = "",
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    try:
        results = store.get_tag_counts(query=q)
        return {"tags": results}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/tags/stats")
def api_tags_stats(
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    try:
        results = store.get_tag_counts(limit=-1)
        return {"tags": results}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
