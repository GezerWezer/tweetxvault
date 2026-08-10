import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.text import Text

from .config import AppConfig, XDGPaths
from .pipeline import current_pipeline
from .rpd import RpdStatus, get_rpd_status, reserve_rpd_request
from .storage.backend import ArchiveStore

MEDIA_TAGGING_SYSTEM_PROMPT = """You will be provided with a tweet (including its author, handle,
text, and attached images or videos). Analyze the content and generate a description
alongside highly specific search tags.

1. **Description:** Provide a concise text description of the media. Capture the primary
subjects, actions, setting, key visual elements, and overall context. You MUST thoroughly
transcribe any prominent text, subtitles, or captions found within the image or video.
2. **Franchise Identification (Primary Tag):** If the content refers to, depicts, or
originates from a specific video game, movie, TV show, anime, or pop culture entity, identify
and include the exact name of that franchise as a tag (e.g., "Deadlock"). Use implicit visual
clues—such as UI elements, art styles, settings, or meme formats—to deduce the correct source
material. Utilize search extensively to verify this.
3. **Specific Entity Formatting:** When tagging specific characters, items, abilities, or
locations, you MUST append the franchise name in parentheses to disambiguate the tag
(e.g., "character name (franchise)").
4. **NO Generic Tags:** Do NOT include broad, categorical, or meta-tags. Exclude terms like
"Video Game", "Gameplay", "Hero Shooter", "MOBA", "Gaming Fail", "Screenshot", or "Funny".
Focus entirely on specific proper nouns, franchises, characters, and distinct subjects.
5. **Platforms Must Be Material:** Do not tag a website, app, social network, or publishing
platform merely because the content was posted or reposted there. Include a platform name only
when the tweet materially discusses it or the media unmistakably represents it through visible
branding, recognizable interface elements, or clearly platform-native content. Incidental
provenance or weak visual resemblance is not enough.
6. **No Redundant Suffixes:** Do NOT append suffixes like "(Franchise)" or "(Video Game)" to
the main franchise tag (e.g., use "Fortnite" instead of "Fortnite (Franchise)").
7. **Existing Tags Preference:** If an intended tag matches or is semantically identical to
one of the user's existing tags, prefer using the exact existing tag to maintain consistency.
You may still create new highly specific tags if no existing tag is appropriate. Existing
tags: {existing_tags}
8. **Tag Limit:** Provide a concise list of exactly 2 to 5 of the most highly relevant tags.
Quality and specificity are more important than quantity.

Ensure that your automated tagging results are clear, relevant, and make the data easily
searchable."""


TEXT_TAGGING_SYSTEM_PROMPT = """You will be provided with text-only tweets, including author
and quoted-post context when present. Generate highly specific search tags for each tweet.

1. Tag the concrete subject matter: named people, organizations, products, franchises, works,
events, technologies, locations, and other distinctive entities or topics.
2. When a specific character, item, ability, or location needs disambiguation, append its
franchise or work in parentheses (for example, "character name (franchise)").
3. Do not create descriptions or summaries. Return tags only.
4. Do not use broad categorical, sentiment, format, or meta-tags such as "Opinion", "Funny",
"News", "Discussion", "Social Media", or "Text Post". Prefer the most specific concepts that
would help the user find this tweet again.
5. Do not tag a website, app, social network, or publishing platform merely because the tweet
was posted there. Include one only when the text materially discusses that platform.
6. If an intended tag matches or is semantically identical to an existing tag, use the exact
existing tag. You may create a new specific tag when needed. Existing tags: {existing_tags}
7. Return exactly 2 to 5 of the most relevant tags for each tweet.

Ensure that the tags are clear, specific, and useful for archive search."""

# Backward-compatible name for callers and tests that import the media prompt directly.
TAGGING_SYSTEM_PROMPT = MEDIA_TAGGING_SYSTEM_PROMPT


class MediaTagResult(BaseModel):
    id: str
    description: str = Field(min_length=1)
    tags: list[str] = Field(min_length=2, max_length=5)


class TextTagResult(BaseModel):
    id: str
    tags: list[str] = Field(min_length=2, max_length=5)


# Backward-compatible result-model name for the original media tagging entry point.
TagResult = MediaTagResult


@dataclass(frozen=True, slots=True)
class TaggingRunResult:
    processed: int = 0
    tagged: int = 0
    batches: int = 0


@dataclass(frozen=True, slots=True)
class _TweetTagContext:
    tweet_id: str
    tweet_type: str
    author_display_name: str
    author_username: str
    text: str


_THINKING_BUDGETS = {
    "none": 0,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
}


def _safe_error(error: BaseException, api_key: str | None) -> str:
    message = str(error)
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message


def _thinking_config(level: str) -> types.ThinkingConfig | None:
    normalized = level.strip().lower()
    fields = getattr(types.ThinkingConfig, "model_fields", {})
    if "thinking_level" in fields:
        return types.ThinkingConfig(thinking_level=normalized)
    if "thinking_budget" in fields:
        return types.ThinkingConfig(
            thinking_budget=_THINKING_BUDGETS.get(normalized, _THINKING_BUDGETS["high"])
        )
    return None


def _mark_failed(store: ArchiveStore, tweet_ids: list[str]) -> None:
    for tweet_id in tweet_ids:
        store.conn.execute(
            "INSERT OR REPLACE INTO archive "
            "(row_key, record_type, tweet_id, raw_json, enrichment_state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"media_tag:{tweet_id}",
                "media_tag",
                tweet_id,
                "{}",
                "failed",
                str(time.time()),
            ),
        )
    store.conn.commit()


def _print_rpd_exhausted(console: Console, status: RpdStatus, model: str) -> None:
    reset_at = status.reset_at.strftime("%Y-%m-%d %H:%M %Z")
    console.print(
        Text(
            f"Gemini daily request limit reached for {model} "
            f"({status.used}/{status.limit}). No further requests will be sent until "
            f"{reset_at}.",
            style="yellow",
        )
    )


def _print_rpd_storage_error(
    console: Console,
    error: BaseException,
    api_key: str | None,
) -> None:
    message = (
        "Could not update Gemini daily request usage; request not sent: "
        f"{_safe_error(error, api_key)}"
    )
    console.print(Text(message, style="red"))


def _print_tag_preview(
    console: Console,
    context: _TweetTagContext,
    *,
    description: str | None,
    tags: list[str],
) -> None:
    author = context.author_display_name
    if context.author_username:
        author = (
            f"{author} (@{context.author_username})" if author else f"@{context.author_username}"
        )
    console.print(Text(f"Tweet {context.tweet_id} ({context.tweet_type})", style="bold"))
    if author:
        console.print(Text(f"Author: {author}"))
    console.print(Text(f"Text: {context.text}"))
    if description:
        console.print(Text(f"Description: {description}"))
    console.print(Text(f"Tags: {', '.join(tags)}"))


def _quote_targets(store: ArchiveStore, tweet_ids: list[str]) -> dict[str, str]:
    if not tweet_ids:
        return {}
    placeholders = ", ".join("?" for _tweet_id in tweet_ids)
    try:
        rows = store.conn.execute(
            f"""
            SELECT tweet_id, target_tweet_id
            FROM archive INDEXED BY idx_archive_tweet_id
            WHERE record_type = 'tweet_relation'
              AND relation_type = 'quote_of'
              AND tweet_id IN ({placeholders})
            ORDER BY tweet_id, target_tweet_id
            """,
            tweet_ids,
        ).fetchall()
    except Exception:
        # Lightweight test stores and legacy callers may not expose relation columns.
        return {}
    targets: dict[str, str] = {}
    for row in rows:
        source_id = row["tweet_id"]
        target_id = row["target_tweet_id"]
        if source_id and target_id:
            targets.setdefault(source_id, target_id)
    return targets


def _content_types_for_tweets(
    store: ArchiveStore,
    tweet_ids: list[str],
    quote_targets: dict[str, str],
) -> dict[str, Literal["media", "text"]]:
    relevant_ids = list(dict.fromkeys([*tweet_ids, *quote_targets.values()]))
    if not relevant_ids:
        return {}
    placeholders = ", ".join("?" for _tweet_id in relevant_ids)
    rows = store.conn.execute(
        f"""
        SELECT DISTINCT tweet_id
        FROM archive INDEXED BY idx_archive_tweet_id
        WHERE record_type = 'media' AND tweet_id IN ({placeholders})
        """,
        relevant_ids,
    ).fetchall()
    media_ids = {row["tweet_id"] for row in rows if row["tweet_id"]}
    return {
        tweet_id: (
            "media"
            if tweet_id in media_ids or quote_targets.get(tweet_id) in media_ids
            else "text"
        )
        for tweet_id in tweet_ids
    }


async def tag_media_tweets(
    store: ArchiveStore,
    config: AppConfig,
    paths: XDGPaths,
    console: Console,
    tweet_ids: list[str],
    model_override: str | None = None,
    *,
    dry_run: bool = False,
    content_type: Literal["media", "text"] | None = None,
    quoted_tweet_ids: dict[str, str] | None = None,
) -> int:
    pipeline = current_pipeline()
    if pipeline is not None and pipeline.has_step("tagging") and not dry_run:
        console = pipeline.capture_console("tagging")  # type: ignore[assignment]
    tag_config = config.tagging
    if not tag_config.enabled or not tag_config.api_key:
        console.print("[yellow]Tagging is disabled or missing API key.[/yellow]")
        return 0

    if not tweet_ids:
        return 0
    if dry_run and len(tweet_ids) != 1:
        raise ValueError("Test tag generation requires exactly one tweet")

    quote_targets = (
        _quote_targets(store, tweet_ids)
        if quoted_tweet_ids is None
        else quoted_tweet_ids
    )
    if content_type is None:
        content_types = _content_types_for_tweets(store, tweet_ids, quote_targets)
        grouped_ids = {
            kind: [tweet_id for tweet_id in tweet_ids if content_types[tweet_id] == kind]
            for kind in ("media", "text")
        }
        nonempty_groups = [kind for kind, ids in grouped_ids.items() if ids]
        if len(nonempty_groups) > 1:
            total = 0
            for kind in nonempty_groups:
                ids = grouped_ids[kind]
                total += await tag_media_tweets(
                    store,
                    config,
                    paths,
                    console,
                    ids,
                    model_override,
                    dry_run=dry_run,
                    content_type=kind,
                    quoted_tweet_ids={
                        tweet_id: quote_targets[tweet_id]
                        for tweet_id in ids
                        if tweet_id in quote_targets
                    },
                )
            return total
        content_type = nonempty_groups[0] if nonempty_groups else "text"

    model_name = model_override or tag_config.model
    if tag_config.rpd is not None:
        try:
            rpd_status = get_rpd_status(
                store,
                model=model_name,
                limit=tag_config.rpd,
            )
        except Exception as error:
            _print_rpd_storage_error(console, error, tag_config.api_key)
            return 0
        if not rpd_status.allowed:
            _print_rpd_exhausted(console, rpd_status, model_name)
            return 0

    try:
        client = genai.Client(api_key=tag_config.api_key)
    except Exception as error:
        console.print(
            f"[red]Could not initialize Gemini: {_safe_error(error, tag_config.api_key)}[/red]"
        )
        return 0

    try:
        for f in client.files.list():
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass
    except Exception as error:
        console.print(
            "[yellow]Warning: Could not clear Gemini File API: "
            f"{_safe_error(error, tag_config.api_key)}[/yellow]"
        )

    all_media: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tweet_objs: dict[str, dict[str, Any]] = {}
    tweet_contexts: dict[str, _TweetTagContext] = {}

    context_ids = list(dict.fromkeys([*tweet_ids, *quote_targets.values()]))
    placeholders = ", ".join("?" for _tweet_id in context_ids)
    media_rows = store.conn.execute(
        f"""
        SELECT tweet_id, media_key, media_type, local_path
        FROM archive INDEXED BY idx_archive_tweet_id
        WHERE record_type = 'media' AND tweet_id IN ({placeholders})
        """,
        context_ids,
    ).fetchall()
    for media_row in media_rows:
        media = dict(media_row)
        all_media[media["tweet_id"]].append(media)

    tweet_rows = store.conn.execute(
        f"""
        SELECT tweet_id, raw_json, author_display_name, author_username, text
        FROM archive INDEXED BY idx_archive_tweet_id
        WHERE record_type = 'tweet_object' AND tweet_id IN ({placeholders})
        """,
        context_ids,
    ).fetchall()
    for tweet_row in tweet_rows:
        tweet = dict(tweet_row)
        tweet_objs[tweet["tweet_id"]] = tweet

    top_tags_list = store.get_tag_counts(limit=50)
    existing_tags_str = (
        ", ".join(f'"{tag["tag"]}"' for tag in top_tags_list)
        if top_tags_list
        else "None yet (create new tags as needed)."
    )
    prompt_template = (
        MEDIA_TAGGING_SYSTEM_PROMPT if content_type == "media" else TEXT_TAGGING_SYSTEM_PROMPT
    )
    system_prompt = prompt_template.format(existing_tags=existing_tags_str)

    generation_parts = [system_prompt, "\n\n--- TWEETS TO TAG ---\n\n"]

    uploaded_videos: list[types.File] = []
    pending_videos: list[tuple[int, types.File, str]] = []
    opened_images: list[Image.Image] = []
    active_media_count = 0
    active_media_by_tweet: dict[str, int] = defaultdict(int)

    try:
        for tid in tweet_ids:
            t_obj = tweet_objs.get(tid)
            if not t_obj:
                continue

            try:
                t_json = json.loads(t_obj.get("raw_json") or "{}")
                if not isinstance(t_json, dict):
                    raise ValueError("tweet JSON is not an object")
            except (json.JSONDecodeError, TypeError, ValueError):
                console.print(
                    f"[yellow]Tweet {tid} has invalid stored tweet JSON; "
                    "using standalone context.[/yellow]"
                )
                t_json = {}
            legacy = t_json.get("legacy") or {}

            author_name = t_obj.get("author_display_name", "")
            author_handle = t_obj.get("author_username", "")
            text = t_obj.get("text", "")

            quoted_tweet_id = quote_targets.get(tid)
            tweet_type = "Standalone"
            if quoted_tweet_id or legacy.get("quoted_status_id_str"):
                tweet_type = "Quote Tweet"
            elif legacy.get("in_reply_to_status_id_str"):
                tweet_type = "Reply"

            tweet_contexts[tid] = _TweetTagContext(
                tweet_id=tid,
                tweet_type=tweet_type,
                author_display_name=author_name,
                author_username=author_handle,
                text=text,
            )

            tweet_text = (
                f"[ID: {tid}]\n"
                f"Type: {tweet_type}\n"
                f"Author: {author_name} (@{author_handle})\n"
                f"Text: {json.dumps(text)}\n"
            )
            if content_type == "media":
                tweet_text += "Media: "
            generation_parts.append(tweet_text)

            quoted_obj = tweet_objs.get(quoted_tweet_id or "")
            if quoted_obj is not None:
                generation_parts.append(
                    "\nQuoted Tweet Context:\n"
                    f"ID: {quoted_tweet_id}\n"
                    f"Author: {quoted_obj.get('author_display_name', '')} "
                    f"(@{quoted_obj.get('author_username', '')})\n"
                    f"Text: {json.dumps(quoted_obj.get('text', ''))}\n"
                )

            if content_type == "text":
                generation_parts.append("\n\n")
                continue

            media_sources = [("Direct Tweet Media", tid)]
            if quoted_tweet_id:
                media_sources.append(("Quoted Tweet Media", quoted_tweet_id))

            for source_label, media_tweet_id in media_sources:
                source_media = all_media.get(media_tweet_id, [])
                if source_media:
                    generation_parts.append(f"\n{source_label}: ")
                for media in source_media:
                    local_path = media.get("local_path")
                    if not local_path:
                        continue

                    abs_path = paths.data_dir / local_path
                    if not abs_path.exists():
                        continue

                    file_size_mb = abs_path.stat().st_size / (1024 * 1024)
                    if file_size_mb > tag_config.max_media_size_mb:
                        console.print(
                            f"[yellow]Skipping media {abs_path.name}: size "
                            f"({file_size_mb:.1f}MB) exceeds limit "
                            f"({tag_config.max_media_size_mb}MB)[/yellow]"
                        )
                        continue

                    media_type = media.get("media_type")
                    if media_type in {"video", "animated_gif"}:
                        try:
                            uploaded = client.files.upload(file=str(abs_path))
                            uploaded_videos.append(uploaded)
                            part_index = len(generation_parts)
                            generation_parts.extend(
                                [
                                    uploaded,
                                    f" [Attached Video: {media.get('media_key')}] ",
                                ]
                            )
                            pending_videos.append((part_index, uploaded, tid))
                        except Exception as error:
                            console.print(
                                f"[red]Failed to upload video {abs_path}: "
                                f"{_safe_error(error, tag_config.api_key)}[/red]"
                            )
                    else:
                        try:
                            image = Image.open(str(abs_path))
                            image.load()
                            opened_images.append(image)
                            generation_parts.append(image)
                            generation_parts.append(
                                f" [Attached Image: {media.get('media_key')}] "
                            )
                            active_media_count += 1
                            active_media_by_tweet[tid] += 1
                        except Exception as error:
                            console.print(
                                f"[red]Failed to load image {abs_path}: "
                                f"{_safe_error(error, tag_config.api_key)}[/red]"
                            )

            generation_parts.append("\n\n")

        if pending_videos:
            console.print(f"Waiting for {len(pending_videos)} videos to process in Gemini...")
            failed_part_indexes: set[int] = set()
            for part_index, video, candidate_tweet_id in pending_videos:
                try:
                    while True:
                        remote = client.files.get(name=video.name)
                        state = remote.state.name
                        if state == "ACTIVE":
                            active_media_count += 1
                            active_media_by_tweet[candidate_tweet_id] += 1
                            break
                        if state == "FAILED":
                            console.print(f"[red]Gemini failed to process video {video.name}[/red]")
                            failed_part_indexes.add(part_index)
                            break
                        await asyncio.sleep(10)
                except Exception as error:
                    console.print(
                        f"[red]Failed while polling video {video.name}: "
                        f"{_safe_error(error, tag_config.api_key)}[/red]"
                    )
                    failed_part_indexes.add(part_index)

            if failed_part_indexes:
                generation_parts = [
                    part
                    for index, part in enumerate(generation_parts)
                    if index not in failed_part_indexes and index - 1 not in failed_part_indexes
                ]

        if not tweet_contexts:
            console.print("[yellow]No stored tweet objects found for the selected IDs.[/yellow]")
            return 0

        if content_type == "media" and active_media_count == 0:
            console.print(
                "[yellow]No loadable media found on disk for the selected tweets.[/yellow]"
            )
            return 0

        console.print(
            Text(
                f"Generating {content_type} tags for {len(tweet_ids)} tweets using "
                f"{model_name}..."
            )
        )

        response = None
        use_search = tag_config.google_search
        for attempt in range(5):
            config_args = {
                "http_options": types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
                "response_mime_type": "application/json",
                "response_schema": list[
                    MediaTagResult if content_type == "media" else TextTagResult
                ],
            }
            if use_search:
                config_args["tools"] = [{"google_search": {}}]
            thinking_config = _thinking_config(tag_config.thinking_level)
            if thinking_config is not None:
                config_args["thinking_config"] = thinking_config
            generation_config = types.GenerateContentConfig(**config_args)

            reservation = None
            if tag_config.rpd is not None:
                try:
                    reservation = reserve_rpd_request(
                        store,
                        model=model_name,
                        limit=tag_config.rpd,
                    )
                except Exception as error:
                    _print_rpd_storage_error(console, error, tag_config.api_key)
                    return 0
                if not reservation.allowed:
                    _print_rpd_exhausted(console, reservation, model_name)
                    return 0

            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=generation_parts,
                    config=generation_config,
                )
                break
            except Exception as error:
                error_text = str(error).upper()
                grounding_failure = any(
                    marker in error_text
                    for marker in (
                        "429",
                        "RESOURCE_EXHAUSTED",
                        "400",
                        "INVALID_ARGUMENT",
                    )
                )
                if grounding_failure and use_search:
                    console.print(
                        "[yellow]Google Search Grounding failed or is incompatible "
                        "with the requested schema (429/400). Retrying immediately "
                        "without Search...[/yellow]"
                    )
                    use_search = False
                    continue

                is_retryable = any(
                    marker in error_text for marker in ("429", "503", "RESOURCE_EXHAUSTED")
                )
                if is_retryable and attempt < 4:
                    if reservation is not None and reservation.remaining == 0:
                        _print_rpd_exhausted(console, reservation, model_name)
                        return 0
                    delay = 15 * (2**attempt)
                    reason = (
                        "429"
                        if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text
                        else "503"
                    )
                    console.print(
                        f"[yellow]Gemini API busy ({reason}). Retrying in {delay} "
                        f"seconds (Attempt {attempt + 1}/5)...[/yellow]"
                    )
                    await asyncio.sleep(delay)
                elif "400" in error_text or "INVALID_ARGUMENT" in error_text:
                    if len(tweet_ids) > 1:
                        if reservation is not None and reservation.remaining == 0:
                            _print_rpd_exhausted(console, reservation, model_name)
                            return 0
                        mid = len(tweet_ids) // 2
                        console.print(
                            f"[yellow]Gemini rejected the payload (400) for batch "
                            f"of {len(tweet_ids)}. Splitting into batches of {mid} "
                            f"and {len(tweet_ids) - mid}...[/yellow]"
                        )
                        total = 0
                        total += await tag_media_tweets(
                            store,
                            config,
                            paths,
                            console,
                            tweet_ids[:mid],
                            model_override,
                            dry_run=dry_run,
                            content_type=content_type,
                            quoted_tweet_ids={
                                tweet_id: quote_targets[tweet_id]
                                for tweet_id in tweet_ids[:mid]
                                if tweet_id in quote_targets
                            },
                        )
                        total += await tag_media_tweets(
                            store,
                            config,
                            paths,
                            console,
                            tweet_ids[mid:],
                            model_override,
                            dry_run=dry_run,
                            content_type=content_type,
                            quoted_tweet_ids={
                                tweet_id: quote_targets[tweet_id]
                                for tweet_id in tweet_ids[mid:]
                                if tweet_id in quote_targets
                            },
                        )
                        return total
                    console.print(
                        "[red]Gemini rejected the payload (400 INVALID_ARGUMENT). "
                        + (
                            "Test result was not saved.[/red]"
                            if dry_run
                            else "Marking tweet as failed.[/red]"
                        )
                    )
                    if not dry_run:
                        _mark_failed(store, tweet_ids)
                    return 0
                else:
                    raise error

        if not response:
            return 0

        response_text = response.text
        if not response_text:
            reason = "Unknown"
            if getattr(response, "candidates", None):
                if getattr(response.candidates[0], "finish_reason", None):
                    reason = str(response.candidates[0].finish_reason)

            if len(tweet_ids) > 1:
                if reservation is not None and reservation.remaining == 0:
                    _print_rpd_exhausted(console, reservation, model_name)
                    return 0
                mid = len(tweet_ids) // 2
                console.print(
                    f"[yellow]Gemini returned an empty response (Reason: {reason}) "
                    f"for batch of {len(tweet_ids)}. Splitting into batches of "
                    f"{mid} and {len(tweet_ids) - mid}...[/yellow]"
                )
                total = 0
                total += await tag_media_tweets(
                    store,
                    config,
                    paths,
                    console,
                    tweet_ids[:mid],
                    model_override,
                    dry_run=dry_run,
                    content_type=content_type,
                    quoted_tweet_ids={
                        tweet_id: quote_targets[tweet_id]
                        for tweet_id in tweet_ids[:mid]
                        if tweet_id in quote_targets
                    },
                )
                total += await tag_media_tweets(
                    store,
                    config,
                    paths,
                    console,
                    tweet_ids[mid:],
                    model_override,
                    dry_run=dry_run,
                    content_type=content_type,
                    quoted_tweet_ids={
                        tweet_id: quote_targets[tweet_id]
                        for tweet_id in tweet_ids[mid:]
                        if tweet_id in quote_targets
                    },
                )
                return total
            console.print(
                f"[red]Gemini returned an empty response (Reason: {reason}). "
                + (
                    "Test result was not saved.[/red]"
                    if dry_run
                    else "Marking tweet as failed.[/red]"
                )
            )
            if not dry_run:
                _mark_failed(store, tweet_ids)
            return 0

        try:
            results = json.loads(response_text)
            if not isinstance(results, list):
                raise ValueError("top-level response must be a list")
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            console.print(
                "[red]Gemini returned an invalid JSON response: "
                f"{_safe_error(error, tag_config.api_key)}[/red]"
            )
            return 0

        tagged_count = 0
        result_model = MediaTagResult if content_type == "media" else TextTagResult
        eligible_result_ids = set(tweet_contexts)
        if content_type == "media":
            eligible_result_ids = {
                tweet_id
                for tweet_id, count in active_media_by_tweet.items()
                if count > 0
            }
        for raw_result in results:
            try:
                result = result_model.model_validate(raw_result)
            except (ValidationError, TypeError, ValueError):
                console.print(
                    "[yellow]Skipping invalid Gemini result that did not match "
                    "the tagging schema.[/yellow]"
                )
                continue

            if result.id not in eligible_result_ids:
                continue

            normalized_tags = [tag.strip().title() for tag in result.tags]
            description = getattr(result, "description", None)
            if dry_run:
                context = tweet_contexts.get(result.id)
                if context is not None:
                    _print_tag_preview(
                        console,
                        context,
                        description=description,
                        tags=normalized_tags,
                    )
                    tagged_count += 1
                continue

            payload_data: dict[str, Any] = {"tags": normalized_tags}
            if description:
                payload_data["description"] = description
            payload = json.dumps(payload_data)
            store.conn.execute(
                "INSERT OR REPLACE INTO archive "
                "(row_key, record_type, tweet_id, raw_json, enrichment_state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"media_tag:{result.id}",
                    "media_tag",
                    result.id,
                    payload,
                    "done",
                    str(time.time()),
                ),
            )
            tagged_count += 1

        if dry_run:
            tweet_label = "tweet" if tagged_count == 1 else "tweets"
            console.print(
                Text(
                    f"Generated tags for {tagged_count} {tweet_label} in test mode; "
                    "no media tags were saved.",
                    style="green",
                )
            )
        else:
            store.conn.commit()
            console.print(f"[green]Successfully tagged {tagged_count} tweets![/green]")
        return tagged_count

    except Exception as error:
        try:
            store.conn.rollback()
        except Exception:
            pass
        console.print(f"[red]Gemini Tagging Failed: {_safe_error(error, tag_config.api_key)}[/red]")
        return 0
    finally:
        for image in opened_images:
            image.close()
        for video in uploaded_videos:
            try:
                client.files.delete(name=video.name)
            except Exception:
                pass


async def tag_pending_media_tweets(
    store: ArchiveStore,
    config: AppConfig,
    paths: XDGPaths,
    console: Console,
    *,
    batch_limit: int | None = None,
    batch_size: int | None = None,
    model_override: str | None = None,
    dry_run: bool = False,
) -> TaggingRunResult:
    """Tag pending text and media tweets until work, quota, or the batch limit is exhausted."""
    if batch_limit is not None and batch_limit < 1:
        raise ValueError("Tagging batch limit must be a positive integer")
    if batch_size is not None and batch_size < 1:
        raise ValueError("Tagging batch size must be a positive integer")

    pipeline = current_pipeline()
    if not config.tagging.enabled or not config.tagging.api_key:
        if pipeline is not None and pipeline.has_step("tagging"):
            reason = (
                "tagging being disabled in configuration"
                if not config.tagging.enabled
                else "no Gemini API key being configured"
            )
            pipeline.skip_step("tagging", reason)
        return TaggingRunResult()

    model_name = model_override or config.tagging.model
    effective_batch_size = (
        batch_size
        if batch_size is not None
        else config.tagging.limit
        if config.tagging.batch
        else 1
    )
    step_key = "tagging"
    if pipeline is not None:
        pipeline.add_step(
            step_key,
            "Tagging",
            total=1,
            unit="tweets",
            detail=f"{model_name} · eligible archived and quoted tweets",
            show_rate=False,
            show_eta=False,
        )
        pipeline.start_step(
            step_key,
            activity="Checking daily quota and preparing the first tag batch",
            counters="0 processed · 0 tagged · 0 batches",
        )
    rpd_status: RpdStatus | None = None
    if pipeline is not None and config.tagging.rpd is not None:
        try:
            rpd_status = get_rpd_status(
                store,
                model=model_name,
                limit=config.tagging.rpd,
            )
        except Exception as error:
            if pipeline is not None:
                pipeline.fail_step(step_key, "could not read Gemini daily request usage")
                pipeline.issue(
                    f"Could not read Gemini daily request usage: "
                    f"{_safe_error(error, config.tagging.api_key)}",
                    level="error",
                    dedupe_key="tagging:rpd-storage",
                )
            else:
                _print_rpd_storage_error(console, error, config.tagging.api_key)
            return TaggingRunResult()
        if not rpd_status.allowed:
            if pipeline is not None:
                reset_at = rpd_status.reset_at.strftime("%Y-%m-%d %H:%M %Z")
                pipeline.skip_step(
                    step_key,
                    f"the Gemini daily request limit being reached until {reset_at}",
                )
                pipeline.issue(
                    f"Gemini daily request limit reached for {model_name} "
                    f"({rpd_status.used}/{rpd_status.limit}); resets {reset_at}.",
                    dedupe_key="tagging:rpd-exhausted",
                )
            else:
                _print_rpd_exhausted(console, rpd_status, model_name)
            return TaggingRunResult()

    effective_batch_limit = 1 if dry_run else batch_limit
    if pipeline is not None and rpd_status is not None:
        effective_batch_limit = (
            min(effective_batch_limit, rpd_status.remaining)
            if effective_batch_limit is not None
            else rpd_status.remaining
        )

    if pipeline is not None:
        effective_total = (
            effective_batch_limit * effective_batch_size
            if effective_batch_limit is not None
            else effective_batch_size
        )
        if dry_run:
            effective_total = 1
        detail = f"{model_name} · batch size {effective_batch_size}"
        if effective_batch_limit is not None:
            detail += f" · up to {effective_total} tweets"
        else:
            detail += " · queue discovered one batch at a time"
        if rpd_status is not None:
            detail += f" · {rpd_status.remaining}/{rpd_status.limit} daily requests available"
        pipeline.add_step(
            step_key,
            "Tagging",
            total=effective_total,
            unit="tweets",
            detail=detail,
            show_rate=False,
            show_eta=False,
        )
        pipeline.start_step(
            step_key,
            activity=f"Preparing the first tag batch for {model_name}",
            counters="0 processed · 0 tagged · 0 batches",
        )
    processed = 0
    tagged = 0
    batches = 0
    attempted_tweet_ids: set[str] = set()
    rich_candidate_selector = hasattr(store, "get_eligible_tagging_candidates")

    while effective_batch_limit is None or batches < effective_batch_limit:
        selection_limit = 1 if dry_run else effective_batch_size

        if pipeline is not None and effective_batch_limit is None and batches > 0:
            effective_total = processed + selection_limit
            pipeline.update_step(step_key, completed=processed, total=effective_total)

        content_type: Literal["media", "text"] = "media"
        quote_targets: dict[str, str] = {}
        if rich_candidate_selector:
            candidates = store.get_eligible_tagging_candidates(
                limit=selection_limit,
                exclude_tweet_ids=attempted_tweet_ids,
            )
            tweet_ids = [candidate["tweet_id"] for candidate in candidates]
            if candidates:
                content_type = candidates[0]["content_type"]
                quote_targets = {
                    candidate["tweet_id"]: candidate["quoted_tweet_id"]
                    for candidate in candidates
                    if candidate.get("quoted_tweet_id")
                }
        else:
            tweet_ids = store.get_eligible_tweets_for_tagging(limit=selection_limit)
        if not tweet_ids:
            break

        if pipeline is not None:
            target_label = tweet_ids[0] if len(tweet_ids) == 1 else f"{len(tweet_ids)} tweets"
            pipeline.update_step(
                step_key,
                completed=processed,
                activity=(
                    f"Generating {content_type} tags for {target_label} with {model_name}"
                ),
                counters=f"{processed} processed · {tagged} tagged · {batches} batches",
            )

        processed += len(tweet_ids)
        tagged_batch = await tag_media_tweets(
            store=store,
            config=config,
            paths=paths,
            console=console,
            tweet_ids=tweet_ids,
            model_override=model_override,
            dry_run=dry_run,
            content_type=content_type,
            quoted_tweet_ids=quote_targets,
        )
        batches += 1
        tagged += tagged_batch
        attempted_tweet_ids.update(tweet_ids)
        if pipeline is not None:
            pipeline.update_step(
                step_key,
                completed=min(processed, effective_total),
                counters=f"{processed} processed · {tagged} tagged · {batches} batches",
                important=True,
            )

        if dry_run:
            break
        if not rich_candidate_selector and (
            tagged_batch < len(tweet_ids) or len(tweet_ids) < selection_limit
        ):
            break

    if pipeline is not None and processed == 0:
        pipeline.skip_step(step_key, "no eligible untagged tweets")
        return TaggingRunResult()
    if pipeline is not None:
        pipeline.update_step(
            step_key,
            completed=max(processed, 1),
            total=max(processed, 1),
        )
        pipeline.complete_step(
            step_key,
            f"{processed} processed · {tagged} tagged · {batches} batches",
        )
    return TaggingRunResult(processed=processed, tagged=tagged, batches=batches)
