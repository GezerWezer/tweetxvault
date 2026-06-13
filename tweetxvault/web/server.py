"""FastAPI web server for tweetxvault."""

import json
import math
import secrets
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.storage import open_archive_store
from tweetxvault.export.common import normalize_collection_name

server_state: dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    if "paths" in server_state:
        server_state["store"] = open_archive_store(server_state["paths"], create=False)
        # Ensure indices exist for fast UI performance
        server_state["store"].ensure_scalar_indexes()
        server_state["store"].ensure_fts_index()
    yield
    if "store" in server_state and server_state["store"]:
        server_state["store"].close()

security = HTTPBasic()
app = FastAPI(title="tweetxvault Web UI", lifespan=lifespan)

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> bool:
    expected_hash = server_state.get("password_hash")
    if not expected_hash:
        return True
        
    input_hash = hashlib.sha256(credentials.password.encode("utf8")).hexdigest()
    is_password_correct = secrets.compare_digest(input_hash, expected_hash)
    
    if not is_password_correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

def get_store():
    return server_state["store"]

def _strip_quotes(s: str) -> str:
    if s.startswith('"') and s.endswith('"'): return s[1:-1]
    return s

def _extract_advanced_filters(q: str | None) -> tuple[dict[str, list[str]], str]:
    token_pattern = re.compile(r'(-?[\w_]+):(\"[^\"]+\"|[^\s]+)|(-?\"[^\"]+\")|([^\s]+)')
    filters = {}
    text_query = []
    
    for match in token_pattern.finditer(q or ""):
        key, val, phrase, word = match.groups()
        if key and val:
            k = key.lower()
            v = _strip_quotes(val).lower()
            if k in filters:
                filters[k].append(v)
            else:
                filters[k] = [v]
        elif phrase: text_query.append(phrase)
        elif word: text_query.append(word)
            
    return filters, " ".join(text_query)

def _parse_twitter_date(date_str: str) -> float | None:
    if not date_str: return None
    try:
        if "_" in date_str:
            parts = date_str.split("_")
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        else:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except Exception: return None

def _apply_advanced_filters(rows: list[dict], filters: dict[str, list[str]]) -> list[dict]:
    if not filters: return rows
    filtered = []
    for r in rows:
        keep = True
        raw = r.get("raw_json") or {}
        legacy = raw.get("legacy") or {}
        
        for k, values in filters.items():
            is_negated = k.startswith('-')
            base_k = k[1:] if is_negated else k
            
            for val in values:
                match = False
                if base_k == "from": match = r.get("author", {}).get("username", "").lower() == val.replace("@", "")
                elif base_k == "to": match = (legacy.get("in_reply_to_screen_name") or "").lower() == val.replace("@", "")
                elif base_k in ("mentions", "@"):
                    mentions = [m.get("screen_name", "").lower() for m in legacy.get("entities", {}).get("user_mentions", [])]
                    match = val.replace("@", "") in mentions
                elif base_k == "since":
                    ts = _parse_twitter_date(val)
                    if ts and r.get("created_at"):
                        try: match = datetime.fromisoformat(r["created_at"].replace('Z', '+00:00')).timestamp() >= ts
                        except Exception: pass
                elif base_k == "until":
                    ts = _parse_twitter_date(val)
                    if ts and r.get("created_at"):
                        try: match = datetime.fromisoformat(r["created_at"].replace('Z', '+00:00')).timestamp() < ts
                        except Exception: pass
                elif base_k == "since_time":
                    try: match = datetime.fromisoformat(r.get("created_at", "").replace('Z', '+00:00')).timestamp() >= float(val)
                    except Exception: pass
                elif base_k == "until_time":
                    try: match = datetime.fromisoformat(r.get("created_at", "").replace('Z', '+00:00')).timestamp() < float(val)
                    except Exception: pass
                elif base_k == "since_id":
                    try: match = int(r.get("tweet_id", 0)) > int(val)
                    except ValueError: pass
                elif base_k == "max_id":
                    try: match = int(r.get("tweet_id", 0)) <= int(val)
                    except ValueError: pass
                elif base_k == "has":
                    if val in ("media", "image", "video"): match = bool(r.get("media"))
                    elif val == "links": match = bool(r.get("urls"))
                elif base_k == "is":
                    if val == "reply": match = bool(legacy.get("in_reply_to_status_id_str"))
                elif base_k == "filter":
                    if val == "replies": match = bool(legacy.get("in_reply_to_status_id_str"))
                    elif val == "quote": match = bool(legacy.get("is_quote_status"))
                    elif val == "nativeretweets": match = bool(legacy.get("retweeted_status_id_str"))
                    elif val == "self_threads": match = (legacy.get("in_reply_to_screen_name") or "").lower() == r.get("author", {}).get("username", "").lower()
                    elif val == "media": match = bool(r.get("media"))
                    elif val == "images": match = any(m.get("type") == "photo" for m in r.get("media", []))
                    elif val == "videos" or val == "native_video": match = any(m.get("type") in ("video", "animated_gif") for m in r.get("media", []))
                    elif val == "links": match = bool(r.get("urls"))
                    elif val == "verified":
                        user_result = raw.get("core", {}).get("user_results", {}).get("result", {})
                        match = user_result.get("is_blue_verified") or user_result.get("legacy", {}).get("verified")
                elif base_k == "min_retweets":
                    try: match = int(legacy.get("retweet_count", 0)) >= int(val)
                    except ValueError: pass
                elif base_k == "min_faves":
                    try: match = int(legacy.get("favorite_count", 0)) >= int(val)
                    except ValueError: pass
                elif base_k == "min_replies":
                    try: match = int(legacy.get("reply_count", 0)) >= int(val)
                    except ValueError: pass
                elif base_k == "conversation_id": match = legacy.get("conversation_id_str") == val
                elif base_k == "quoted_tweet_id": match = legacy.get("quoted_status_id_str") == val
                elif base_k == "url": match = any(val in (u.get("expanded_url") or "").lower() or val in (u.get("display_url") or "").lower() for u in r.get("urls", []))
                elif base_k == "source": match = val.replace("_", " ") in raw.get("source", "").lower()
                elif base_k == "card_name": match = raw.get("card", {}).get("name") == val
                else: match = True
                    
                if is_negated:
                    if match: keep = False
                else:
                    if not match: keep = False
                    
                if not keep: break
            if not keep: break
        if keep: filtered.append(r)
    return filtered

@app.get("/api/tweets")
def api_tweets(
    q: str | None = None,
    collection: str = Query("all"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    """Fetch tweets with pagination, collection filtering, and advanced search."""
    try:
        try:
            internal_col = normalize_collection_name(collection)
        except ValueError:
            internal_col = "all"
            
        filters, text_query = _extract_advanced_filters(q)
        
        start = (page - 1) * limit
        end = start + limit

        pushable_exprs = []
        post_filters = {}
        for k, v in filters.items():
            base_k = k[1:] if k.startswith('-') else k
            if not k.startswith('-') and base_k in {"from", "conversation_id"}:
                if base_k == "from":
                    vals = [val.replace("@", "") for val in v]
                    joined = " OR ".join(f"LOWER(author_username) = '{val}'" for val in vals)
                    pushable_exprs.append(f"({joined})")
                elif base_k == "conversation_id":
                    joined = " OR ".join(f"conversation_id = '{val}'" for val in v)
                    pushable_exprs.append(f"({joined})")
            else:
                post_filters[k] = v

        paginated_tweets = []
        total = 0

        if not post_filters and not text_query and not pushable_exprs:
            # Fast path
            sorted_ids = store.get_sorted_tweet_ids(internal_col, sort="newest")
            total = len(sorted_ids)
            paginated_tweets = store.fetch_tweets_by_ids(sorted_ids[start:end])
            
        elif not post_filters and not text_query and pushable_exprs:
            # Medium path: Pushable filters only
            filter_expr = "record_type = 'tweet'"
            if internal_col != "all":
                filter_expr += f" AND collection_type = '{internal_col}'"
            for expr in pushable_exprs:
                filter_expr += f" AND {expr}"
                
            tweet_rows = store.table.search().where(filter_expr).select(["tweet_id", "created_at", "sort_index"]).to_list()
            
            def sort_index_val(row):
                try: return int(row.get("sort_index") or 0)
                except: return 0
                
            def newest_key(row):
                try:
                    dt = datetime.strptime(row.get("created_at") or "", "%a %b %d %H:%M:%S %z %Y")
                    return (0, -dt.timestamp(), -sort_index_val(row), row.get("tweet_id") or "")
                except: return (1, 0.0, -sort_index_val(row), row.get("tweet_id") or "")
                    
            tweet_rows.sort(key=newest_key)
            total = len(tweet_rows)
            page_ids = [r["tweet_id"] for r in tweet_rows[start:end] if r.get("tweet_id")]
            paginated_tweets = store.fetch_tweets_by_ids(page_ids)
            
        elif not post_filters and text_query and not pushable_exprs:
            # Semi-fast path
            coll_set = {internal_col} if internal_col != "all" else None
            search_results = store.search_fts(text_query, limit=1000, collections=coll_set)
            total = len(search_results)
            page_results = search_results[start:end]
            page_ids = [r["tweet_id"] for r in page_results if r.get("tweet_id")]
            paginated_tweets = store.fetch_tweets_by_ids(page_ids)
            
        else:
            # Slow path: post filters and/or combinations
            all_rows = store.export_rows(internal_col, sort="newest", include_raw_json=True)
            if text_query:
                coll_set = {internal_col} if internal_col != "all" else None
                search_results = store.search_fts(text_query, limit=1000, collections=coll_set)
                matched_ids = {r["tweet_id"] for r in search_results}
                all_rows = [r for r in all_rows if r["tweet_id"] in matched_ids]
                order = {r["tweet_id"]: i for i, r in enumerate(search_results)}
                all_rows.sort(key=lambda x: order.get(x["tweet_id"], 9999))
                
            filtered_rows = _apply_advanced_filters(all_rows, filters)
            total = len(filtered_rows)
            paginated_tweets = filtered_rows[start:end]

        # Batch QT media query
        qt_ids = set()
        for r in paginated_tweets:
            raw = r.get("raw_json", {})
            if isinstance(raw, dict):
                quote = raw.get("quoted_status_result", {}).get("result")
                if isinstance(quote, dict):
                    if quote.get("__typename") == "TweetWithVisibilityResults":
                        quote = quote.get("tweet", {})
                    qt_id = quote.get("rest_id")
                    if qt_id:
                        qt_ids.add(qt_id)
                        
        if qt_ids:
            qt_media_rows = store._rows_for_values("media", "tweet_id", list(qt_ids))
            qt_media_by_id = {}
            for m in qt_media_rows:
                tid = m.get("tweet_id")
                if tid:
                    qt_media_by_id.setdefault(tid, []).append(m)
                    
            for r in paginated_tweets:
                raw = r.get("raw_json", {})
                if isinstance(raw, dict):
                    quote = raw.get("quoted_status_result", {}).get("result")
                    if isinstance(quote, dict):
                        if quote.get("__typename") == "TweetWithVisibilityResults":
                            quote = quote.get("tweet", {})
                        qt_id = quote.get("rest_id")
                        if qt_id and qt_id in qt_media_by_id:
                            r["qt_media"] = [
                                {
                                    "type": m.get("media_type"),
                                    "duration_millis": m.get("duration_millis"),
                                    "download": {
                                        "local_path": m.get("local_path"),
                                        "thumbnail_local_path": m.get("thumbnail_local_path")
                                    }
                                } for m in qt_media_by_id[qt_id][:10]
                            ]

        return {
            "tweets": paginated_tweets,
            "total": total,
            "page": page,
            "pages": math.ceil(total / limit) if total > 0 else 1
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/avatar/{user_id}")
def get_avatar(user_id: str, store = Depends(get_store), _auth: bool = Depends(verify_credentials)):
    paths: XDGPaths = server_state["paths"]
    avatars_dir = paths.media_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    
    avatar_path = avatars_dir / f"{user_id}.jpg"
    if avatar_path.exists():
        return FileResponse(avatar_path)
        
    rows = store.table.search().where(f"author_id = '{user_id}' AND record_type = 'tweet'").limit(1).to_list()
    if not rows:
        rows = store.table.search().where(f"author_id = '{user_id}' AND record_type = 'tweet_object'").limit(1).to_list()
        
    if rows and rows[0].get("raw_json"):
        config = server_state.get("config")
        if not config or not config.web.fetch_avatars:
            raise HTTPException(status_code=404, detail="Avatar fetching disabled")
        try:
            raw = json.loads(rows[0]["raw_json"])
            user_res = raw.get("core", {}).get("user_results", {}).get("result", {})
            
            url = user_res.get("avatar", {}).get("image_url")
            if not url:
                url = user_res.get("legacy", {}).get("profile_image_url_https")
            
            if url:
                url = url.replace("_normal", "_400x400")
                resp = httpx.get(url, timeout=10.0)
                if resp.status_code == 200:
                    avatar_path.write_bytes(resp.content)
                    return FileResponse(avatar_path)
        except Exception as e:
            pass
            
    raise HTTPException(status_code=404, detail="Avatar not found")

@app.get("/api/tweets/{tweet_id}")
def api_tweet_thread(
    tweet_id: str, 
    store = Depends(get_store), 
    _auth: bool = Depends(verify_credentials)
):
    try:
        # 1. Fetch direct relationships touching the focal tweet
        relations = store.table.search().where(
            f"record_type = 'tweet_relation' AND (tweet_id = '{tweet_id}' OR target_tweet_id = '{tweet_id}')"
        ).limit(100).to_list()
        
        related_ids = {tweet_id}
        for r in relations:
            related_ids.add(r["tweet_id"])
            related_ids.add(r["target_tweet_id"])
            
        # Extract child candidates from initial relations to find potential sub-threads
        child_candidates = set()
        for r in relations:
            if r.get("relation_type") in ("reply_to", "thread_parent") and r.get("target_tweet_id") == tweet_id:
                child_candidates.add(r.get("tweet_id"))
            elif r.get("relation_type") == "thread_child" and r.get("tweet_id") == tweet_id:
                child_candidates.add(r.get("target_tweet_id"))
                
        # 2. Fetch sub-relationships to capture replies to our children
        sub_relations = []
        if child_candidates:
            child_id_list = ", ".join(f"'{cid}'" for cid in child_candidates if cid)
            sub_relations = store.table.search().where(
                f"record_type = 'tweet_relation' AND target_tweet_id IN ({child_id_list})"
            ).limit(100).to_list()
            for sr in sub_relations:
                related_ids.add(sr["tweet_id"])
                related_ids.add(sr["target_tweet_id"])
                
        all_relations = relations + sub_relations
        
        id_list = ", ".join(f"'{tid}'" for tid in related_ids)
        objs = store.table.search().where(f"record_type = 'tweet_object' AND tweet_id IN ({id_list})").limit(100).to_list()
        media = store.table.search().where(f"record_type = 'media' AND tweet_id IN ({id_list})").limit(100).to_list()
        col_rows = store.table.search().where(f"record_type = 'tweet' AND tweet_id IN ({id_list})").limit(100).to_list()
        
        col_dict = {}
        for c in col_rows:
            col_dict.setdefault(c["tweet_id"], []).append(c["collection_type"])

        # Batch QT Media lookup
        qt_ids = set()
        for obj in objs:
            if obj.get("raw_json"):
                raw_json = json.loads(obj["raw_json"])
                if isinstance(raw_json, dict):
                    quote = raw_json.get("quoted_status_result", {}).get("result")
                    if isinstance(quote, dict):
                        if quote.get("__typename") == "TweetWithVisibilityResults":
                            quote = quote.get("tweet", {})
                        qt_id = quote.get("rest_id")
                        if qt_id:
                            qt_ids.add(qt_id)
                            
        qt_media_by_id = {}
        if qt_ids:
            qt_media_rows = store._rows_for_values("media", "tweet_id", list(qt_ids))
            for m in qt_media_rows:
                tid = m.get("tweet_id")
                if tid:
                    qt_media_by_id.setdefault(tid, []).append(m)
        
        formatted = {}
        for obj in objs:
            tid = obj["tweet_id"]
            t_media = [m for m in media if m.get("tweet_id") == tid]
            raw_json = json.loads(obj["raw_json"]) if obj.get("raw_json") else None
            
            qt_media_formatted = []
            if raw_json and isinstance(raw_json, dict):
                quote = raw_json.get("quoted_status_result", {}).get("result")
                if isinstance(quote, dict):
                    if quote.get("__typename") == "TweetWithVisibilityResults":
                        quote = quote.get("tweet", {})
                    qt_id = quote.get("rest_id")
                    if qt_id and qt_id in qt_media_by_id:
                        qt_media_formatted = [
                            {
                                "type": m.get("media_type"),
                                "duration_millis": m.get("duration_millis"),
                                "download": {
                                    "local_path": m.get("local_path"),
                                    "thumbnail_local_path": m.get("thumbnail_local_path")
                                }
                            } for m in qt_media_by_id[qt_id][:10]
                        ]

            formatted[tid] = {
                "tweet_id": tid,
                "text": obj.get("text", ""),
                "collections": col_dict.get(tid, []),
                "author": {
                    "id": obj.get("author_id"),
                    "username": obj.get("author_username"),
                    "display_name": obj.get("author_display_name")
                },
                "created_at": obj.get("created_at"),
                "synced_at": obj.get("synced_at"),
                "media": [
                    {
                        "type": m.get("media_type"),
                        "duration_millis": m.get("duration_millis"),
                        "download": {
                            "local_path": m.get("local_path"),
                            "thumbnail_local_path": m.get("thumbnail_local_path")
                        }
                    } for m in t_media
                ],
                "raw_json": raw_json,
                "qt_media": qt_media_formatted
            }
            
        main_tweet = formatted.get(tweet_id)
        if not main_tweet:
            raise HTTPException(status_code=404, detail="Tweet not found")
            
        parents, children_map = [], {}
        seen_parents = set()
        
        # Resolve Parents and Direct Children
        for r in all_relations:
            rel_type = r.get("relation_type")
            src = r.get("tweet_id")
            tgt = r.get("target_tweet_id")
            
            if src == tweet_id and rel_type in ("reply_to", "thread_parent"):
                if tgt in formatted and tgt not in seen_parents:
                    parents.append(formatted[tgt])
                    seen_parents.add(tgt)
            elif tgt == tweet_id and rel_type == "thread_child":
                if src in formatted and src not in seen_parents:
                    parents.append(formatted[src])
                    seen_parents.add(src)
                    
            elif tgt == tweet_id and rel_type in ("reply_to", "thread_parent"):
                if src in formatted and src not in children_map:
                    children_map[src] = formatted[src]
                    children_map[src]["op_replies"] = []
            elif src == tweet_id and rel_type == "thread_child":
                if tgt in formatted and tgt not in children_map:
                    children_map[tgt] = formatted[tgt]
                    children_map[tgt]["op_replies"] = []

        # Map sub-replies authored by the original Thread OP (Main Author) to their parent reply
        main_author_id = main_tweet["author"]["id"]
        for r in all_relations:
            rel_type = r.get("relation_type")
            src = r.get("tweet_id")
            tgt = r.get("target_tweet_id")
            
            if tgt in children_map and rel_type in ("reply_to", "thread_parent"):
                grandchild = formatted.get(src)
                if grandchild and grandchild["author"]["id"] == main_author_id:
                    if grandchild not in children_map[tgt]["op_replies"]:
                        children_map[tgt]["op_replies"].append(grandchild)
                    
        parents.sort(key=lambda x: x["created_at"] or "")
        
        # Convert children map to list
        children = list(children_map.values())
        
        def get_likes(t):
            raw = t.get("raw_json", {}) or {}
            return int(raw.get("legacy", {}).get("favorite_count", 0))
            
        # Sort children: Threads containing OP replies first, then fallback to descending Likes
        children.sort(key=lambda x: (len(x.get("op_replies", [])) > 0, get_likes(x)), reverse=True)
        
        return {
            "main": main_tweet,
            "parents": parents,
            "children": children
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats")
def api_stats(
    store = Depends(get_store),
    _auth: bool = Depends(verify_credentials)
):
    stats = store.archive_stats()
    return {
        "latest_sync_at": stats.latest_sync_at
    }


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