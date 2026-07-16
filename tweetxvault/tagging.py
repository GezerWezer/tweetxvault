import json
import time
import asyncio
from typing import Any, Mapping
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from pydantic import BaseModel, Field

from google import genai
from google.genai import types
from PIL import Image

from .config import AppConfig, TaggingConfig, XDGPaths
from .storage.backend import ArchiveStore

TAGGING_SYSTEM_PROMPT = """You will be provided with a tweet (including its author, handle, text, and attached images or videos). Your task is to analyze the content and generate a description alongside highly specific search tags.

1. **Description:** Provide a concise text description of the media. Capture the primary subjects, actions, setting, key visual elements, overall context, and transcribe any prominent text.
2. **Franchise Identification (Primary Tag):** If the content refers to, depicts, or originates from a specific video game, movie, TV show, anime, or pop culture entity, identify and include the exact name of that franchise as a tag (e.g., "Deadlock"). Use implicit visual clues—such as UI elements, art styles, settings, or meme formats—to deduce the correct source material. Utilize search extensively to verify this.
3. **Specific Entity Formatting:** When tagging specific characters, items, abilities, or locations, you MUST append the franchise name in parentheses to disambiguate the tag (e.g., "character name (franchise)"). 
4. **NO Generic Tags:** Do NOT include broad, categorical, or meta-tags. Exclude terms like "Video Game", "Gameplay", "Hero Shooter", "MOBA", "Gaming Fail", "Screenshot", or "Funny". Focus entirely on specific proper nouns, franchises, characters, and distinct subjects.
5. **Tag Limit:** Provide a concise list of exactly 2 to 5 of the most highly relevant tags. Quality and specificity are more important than quantity.

Ensure that your automated tagging results are clear, relevant, and make the data easily searchable."""


class TagResult(BaseModel):
    id: str
    description: str
    tags: list[str]


async def tag_media_tweets(
    store: ArchiveStore,
    config: AppConfig,
    paths: XDGPaths,
    console: Console,
    tweet_ids: list[str],
    model_override: str | None = None,
) -> int:
    tag_config = config.tagging
    if not tag_config.enabled or not tag_config.api_key:
        console.print("[yellow]Tagging is disabled or missing API key.[/yellow]")
        return 0

    if not tweet_ids:
        return 0

    client = genai.Client(api_key=tag_config.api_key)
    
    all_media: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tweet_objs: dict[str, dict[str, Any]] = {}
    
    for tid in tweet_ids:
        media_rows = store.conn.execute("SELECT * FROM archive WHERE record_type = 'media' AND tweet_id = ?", (tid,)).fetchall()
        for m in media_rows:
            all_media[tid].append(dict(m))
            
        tweet_row = store.conn.execute("SELECT * FROM archive WHERE record_type = 'tweet_object' AND tweet_id = ?", (tid,)).fetchone()
        if tweet_row:
            tweet_objs[tid] = dict(tweet_row)

    generation_parts = [TAGGING_SYSTEM_PROMPT, "\n\n--- TWEETS TO TAG ---\n\n"]
    
    uploaded_videos: list[types.File] = []
    has_media = False
    
    for tid in tweet_ids:
        t_obj = tweet_objs.get(tid)
        if not t_obj:
            continue
            
        t_json = json.loads(t_obj.get("raw_json") or "{}")
        legacy = t_json.get("legacy") or {}
        
        author_name = t_obj.get("author_display_name", "")
        author_handle = t_obj.get("author_username", "")
        text = t_obj.get("text", "")
        
        tweet_type = "Standalone"
        if legacy.get("quoted_status_id_str"):
            tweet_type = "Quote Tweet"
        elif legacy.get("in_reply_to_status_id_str"):
            tweet_type = "Reply"

        tweet_text = f"[ID: {tid}]\nType: {tweet_type}\nAuthor: {author_name} (@{author_handle})\nText: {json.dumps(text)}\nMedia: "
        generation_parts.append(tweet_text)
        
        for m in all_media.get(tid, []):
            local_path = m.get("local_path")
            if not local_path:
                continue
                
            abs_path = paths.media_dir / local_path
            if not abs_path.exists():
                continue
                
            m_type = m.get("media_type")
            if m_type == "video" or m_type == "animated_gif":
                try:
                    f = client.files.upload(file=str(abs_path))
                    uploaded_videos.append(f)
                    generation_parts.append(f)
                    generation_parts.append(f" [Attached Video: {m.get('media_key')}] ")
                    has_media = True
                except Exception as e:
                    console.print(f"[red]Failed to upload video {abs_path}: {e}[/red]")
            else:
                try:
                    img = Image.open(str(abs_path))
                    img.load()
                    generation_parts.append(img)
                    generation_parts.append(f" [Attached Image: {m.get('media_key')}] ")
                    has_media = True
                except Exception as e:
                    console.print(f"[red]Failed to load image {abs_path}: {e}[/red]")
                    
        generation_parts.append("\n\n")

    if not has_media:
        console.print("[yellow]No loadable media found for the selected tweets.[/yellow]")
        return 0

    if uploaded_videos:
        console.print(f"Waiting for {len(uploaded_videos)} videos to process in Gemini...")
        for v in uploaded_videos:
            while True:
                f = client.files.get(name=v.name)
                if f.state.name == "ACTIVE":
                    break
                elif f.state.name == "FAILED":
                    console.print(f"[red]Gemini failed to process video {v.name}[/red]")
                    break
                await asyncio.sleep(2)

    model_name = model_override or tag_config.model
    console.print(f"Generating tags for {len(tweet_ids)} tweets using {model_name}...")
    
    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=generation_parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[TagResult],
                tools=[{"google_search": {}}],
            )
        )
        
        res_json = response.text
        results = json.loads(res_json)
        
        tagged_count = 0
        for r in results:
            tid = str(r["id"])
            if tid in tweet_ids:
                tags = [t.title() for t in r["tags"]]
                payload = json.dumps({
                    "description": r["description"],
                    "tags": tags
                })
                
                store.conn.execute(
                    "INSERT OR REPLACE INTO archive (row_key, record_type, tweet_id, raw_json, enrichment_state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (f"media_tag:{tid}", "media_tag", tid, payload, "done", str(time.time()))
                )
                tagged_count += 1
                
        store.conn.commit()
        console.print(f"[green]Successfully tagged {tagged_count} tweets![/green]")
        return tagged_count
        
    except Exception as e:
        console.print(f"[red]Gemini Tagging Failed: {e}[/red]")
        return 0
    finally:
        for v in uploaded_videos:
            try:
                client.files.delete(name=v.name)
            except:
                pass
