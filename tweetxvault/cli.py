"""Typer CLI entrypoint."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from loguru import logger
from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from tweetxvault import __version__
from tweetxvault.archive_import import (
    ArchiveEnrichmentAborted,
    ArchiveEnrichmentInterrupted,
    enrich_imported_archive,
    import_x_archive,
)
from tweetxvault.articles import refresh_articles
from tweetxvault.auth import (
    BrowserCandidate,
    list_available_browser_candidates,
    resolve_auth_bundle,
)
from tweetxvault.config import ensure_paths, load_config
from tweetxvault.exceptions import ConfigError, ProcessLockError, TweetXVaultError
from tweetxvault.export import export_html_archive, export_json_archive
from tweetxvault.export.common import (
    default_export_path,
    display_collection_name,
    normalize_collection_name,
)
from tweetxvault.extractor import extract_status_id_from_url
from tweetxvault.grailbird import convert_archive as convert_grailbird_archive
from tweetxvault.media import download_media
from tweetxvault.pipeline import PipelineReporter, current_pipeline
from tweetxvault.query_ids import QueryIdStore, refresh_query_ids
from tweetxvault.reminders import print_pending_archive_enrichment_reminder
from tweetxvault.storage import open_archive_store
from tweetxvault.sync import (
    ProcessLock,
    SyncFollowupPlan,
    run_preflight,
    sync_all,
    sync_collection,
)
from tweetxvault.threads import expand_threads
from tweetxvault.unfurl import unfurl_urls

app = typer.Typer(no_args_is_help=True)
article_app = typer.Typer(no_args_is_help=True, help="Refresh archived article bodies.")
auth_app = typer.Typer(no_args_is_help=True, help="Check auth and refresh query IDs.")
db_app = typer.Typer(no_args_is_help=True, help="Inspect the local SQLite archive database.")
export_app = typer.Typer(no_args_is_help=True, help="Export the local archive.")
import_app = typer.Typer(no_args_is_help=True, help="Import and enrich official X archives.")
media_app = typer.Typer(no_args_is_help=True, help="Download archived tweet media.")
repair_app = typer.Typer(no_args_is_help=True, help="Repair recoverable legacy archive rows.")
SYNC_GROUP_HELP = (
    "Run the normal sync pass. Without a subcommand, this syncs bookmarks and likes, "
    "then runs thread expansion, resurrection checks, article refresh, media download, "
    "URL unfurl, and configured media tagging unless skipped or inapplicable."
)
SYNC_ALL_HELP = (
    "Sync bookmarks and likes, then run thread expansion, resurrection checks, article "
    "refresh, media download, URL unfurl, and configured media tagging unless skipped "
    "or inapplicable."
)
sync_app = typer.Typer(
    invoke_without_command=True,
    help=SYNC_GROUP_HELP,
)
thread_app = typer.Typer(no_args_is_help=True, help="Expand archived tweet threads.")
view_app = typer.Typer(no_args_is_help=True, help="Render archived tweets in the terminal.")

app.add_typer(article_app, name="articles", help="Refresh archived article bodies.")
app.add_typer(auth_app, name="auth", help="Check auth and refresh query IDs.")
app.add_typer(db_app, name="db", help="Inspect the local SQLite archive database.")
app.add_typer(export_app, name="export", help="Export the local archive.")
app.add_typer(import_app, name="import", help="Import and enrich official X archives.")
app.add_typer(media_app, name="media", help="Download archived tweet media.")
app.add_typer(repair_app, name="repair", help="Repair recoverable legacy archive rows.")
app.add_typer(
    sync_app,
    name="sync",
    help=SYNC_GROUP_HELP,
)
app.add_typer(thread_app, name="threads", help="Expand archived tweet threads.")
app.add_typer(view_app, name="view", help="Render archived tweets in the terminal.")

# Web UI management (optional dependency)
try:
    from tweetxvault.cli_web import web_app

    app.add_typer(web_app, name="web", help="Manage the background web UI server.")
except ImportError:
    pass


BROWSER_HELP = (
    "Browser to use for cookie extraction: firefox, chrome, chromium, brave, edge, "
    "opera, opera-gx, vivaldi, arc."
)
DEBUG_AUTH_HELP = "Print browser/profile auth-resolution diagnostics."
ARTICLE_BACKFILL_HELP = (
    "Rewalk existing timeline pages without resetting sync state so older items can pick up "
    "new article fields."
)
SYNC_FULL_HELP = "Reset saved sync state for the targeted collection before syncing."
SYNC_BACKFILL_HELP = "Continue older history past duplicates without resetting sync state."
HEAD_ONLY_HELP = (
    "Clear any saved backfill cursor for the targeted collection and run only the head pass. "
    "Does not resume older historical backfill state."
)
SYNC_LIMIT_HELP = "Maximum number of pages to fetch for this run."
SYNC_SKIP_RESURRECTION_HELP = "Skip bounded unavailable-tweet resurrection checks after sync."
SYNC_SKIP_ARTICLES_HELP = "Skip automatic article-body refresh after sync."
SYNC_SKIP_MEDIA_HELP = "Skip automatic media downloads after sync."
SYNC_SKIP_UNFURL_HELP = "Skip automatic URL unfurls after sync."
SYNC_SKIP_THREADS_HELP = "Skip automatic thread expansion after sync."
SYNC_BROWSER_OPTION = Annotated[str | None, typer.Option("--browser", help=BROWSER_HELP)]
SYNC_PROFILE_OPTION = Annotated[
    str | None,
    typer.Option("--profile", help="Browser profile name or directory name."),
]
SYNC_PROFILE_PATH_OPTION = Annotated[
    Path | None,
    typer.Option("--profile-path", help="Explicit browser profile directory path."),
]
SYNC_ARTICLE_BACKFILL_OPTION = Annotated[
    bool,
    typer.Option("--article-backfill", help=ARTICLE_BACKFILL_HELP),
]
SYNC_HEAD_ONLY_OPTION = Annotated[
    bool,
    typer.Option("--head-only", help=HEAD_ONLY_HELP),
]
SYNC_SKIP_RESURRECTION_OPTION = Annotated[
    bool,
    typer.Option("--skip-resurrection", help=SYNC_SKIP_RESURRECTION_HELP),
]
SYNC_SKIP_ARTICLES_OPTION = Annotated[
    bool,
    typer.Option("--skip-articles", help=SYNC_SKIP_ARTICLES_HELP),
]
SYNC_SKIP_MEDIA_OPTION = Annotated[
    bool,
    typer.Option("--skip-media", help=SYNC_SKIP_MEDIA_HELP),
]
SYNC_SKIP_UNFURL_OPTION = Annotated[
    bool,
    typer.Option("--skip-unfurl", help=SYNC_SKIP_UNFURL_HELP),
]
SYNC_SKIP_THREADS_OPTION = Annotated[
    bool, typer.Option("--skip-threads", help="Skip thread expansion pass.")
]
SYNC_MAX_LINKED_DEPTH_OPTION = Annotated[
    int | None,
    typer.Option(
        "--max-linked-depth",
        help="Maximum degrees of separation for linked-status discovery.",
    ),
]
DEBUG_AUTH_OPTION = Annotated[
    bool,
    typer.Option("--debug-auth", help=DEBUG_AUTH_HELP),
]


SEARCH_TYPE_HELP = "Comma-delimited search result types: post, article."
SEARCH_COLLECTION_HELP = "Comma-delimited collections: bookmark, like, tweet."
SEARCH_SORT_HELP = "Search result sort: relevance, newest, oldest."
ARTICLE_LIMIT_HELP = "Maximum number of archived article rows or explicit targets to process."
THREAD_LIMIT_HELP = "Maximum number of archived thread targets to process."
VIEW_LIMIT_HELP = "Maximum number of rows to display."
VIEW_SORT_HELP = "Display order: newest or oldest."
EXPORT_COLLECTION_HELP = "Collection to export: bookmarks, likes, tweets, or all."
EXPORT_OUT_HELP = "Destination file path. Defaults to the exports/ directory."
MEDIA_LIMIT_HELP = "Maximum number of pending media rows to process."
MEDIA_PHOTOS_ONLY_HELP = "Only download photo rows and skip video or animated GIF media."
RETRY_FAILED_HELP = "Retry rows that previously failed instead of only untouched pending rows."
UNFURL_LIMIT_HELP = "Maximum number of saved URL rows to fetch metadata for."
TAG_LIMIT_HELP = "Maximum number of tweets to tag in this run."
SEARCH_QUERY_HELP = "Search query text."
SEARCH_LIMIT_HELP = "Maximum number of results to return."
# Keep the user-facing flag as --type, but map it onto internal search-result kinds so
# search code does not collide with storage-level record_type/type terminology.
SEARCH_TYPE_ALIASES = {
    "post": "post",
    "posts": "post",
    "article": "article",
    "articles": "article",
}
SEARCH_SORT_OPTION = Annotated[
    Literal["relevance", "newest", "oldest"],
    typer.Option("--sort", help=SEARCH_SORT_HELP),
]
ARTICLE_LIMIT_OPTION = Annotated[int | None, typer.Option("--limit", help=ARTICLE_LIMIT_HELP)]
THREAD_LIMIT_OPTION = Annotated[int | None, typer.Option("--limit", help=THREAD_LIMIT_HELP)]
VIEW_LIMIT_OPTION = Annotated[int, typer.Option("--limit", help=VIEW_LIMIT_HELP)]
VIEW_SORT_OPTION = Annotated[str, typer.Option("--sort", help=VIEW_SORT_HELP)]
EXPORT_COLLECTION_OPTION = Annotated[str, typer.Option("--collection", help=EXPORT_COLLECTION_HELP)]
EXPORT_OUT_OPTION = Annotated[Path | None, typer.Option("--out", help=EXPORT_OUT_HELP)]
MEDIA_LIMIT_OPTION = Annotated[int | None, typer.Option("--limit", help=MEDIA_LIMIT_HELP)]
PHOTOS_ONLY_OPTION = Annotated[
    bool,
    typer.Option("--photos-only", help=MEDIA_PHOTOS_ONLY_HELP),
]
RETRY_FAILED_OPTION = Annotated[
    bool,
    typer.Option("--retry-failed", help=RETRY_FAILED_HELP),
]
UNFURL_LIMIT_OPTION = Annotated[int | None, typer.Option("--limit", help=UNFURL_LIMIT_HELP)]
TAG_LIMIT_OPTION = Annotated[
    int | None,
    typer.Option("--limit", min=1, help=TAG_LIMIT_HELP),
]
SEARCH_QUERY_ARGUMENT = Annotated[str, typer.Argument(help=SEARCH_QUERY_HELP)]
SEARCH_LIMIT_OPTION = Annotated[int, typer.Option("--limit", help=SEARCH_LIMIT_HELP)]


def _configure_logging() -> Console:
    logger.remove()
    service_mode = bool(os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"))
    logger.add(sys.stderr, level="INFO", format="{message}", colorize=not service_mode)
    return Console(
        stderr=True,
        force_terminal=False if service_mode else None,
        color_system=None if service_mode else "auto",
    )


def _find_git_repo_root() -> Path | None:
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_command_output(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = result.stdout.strip()
    return output or None


def _version_text() -> str:
    version = f"tweetxvault {__version__}"
    repo_root = _find_git_repo_root()
    if repo_root is None:
        return version
    revision = _git_command_output(repo_root, "rev-parse", "--short", "HEAD")
    if revision is None:
        return version
    status = _git_command_output(repo_root, "status", "--short", "--untracked-files=no")
    suffix = f" ({revision}"
    if status:
        suffix += ", dirty"
    suffix += ")"
    return version + suffix


def _version_callback(
    ctx: typer.Context,
    _param: typer.CallbackParam,
    value: bool,
) -> None:
    if not value or ctx.resilient_parsing:
        return
    typer.echo(_version_text())
    raise typer.Exit()


def _browser_cookie_only_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("TWEETXVAULT_AUTH_TOKEN", "TWEETXVAULT_CT0"):
        env.pop(key, None)
    return env


def _auth_status_callback(console: Console, *, enabled: bool):
    if not enabled:
        return None
    pipeline = current_pipeline()
    if pipeline is not None:
        return lambda message: pipeline.detail("auth", message)
    return lambda message: console.print(f"auth: {message}", highlight=False)


def _pick_browser_candidate_interactively(
    console: Console,
    *,
    browser: str | None,
) -> BrowserCandidate:
    candidates = list_available_browser_candidates(browser=browser)
    if not candidates:
        scope = f" for {browser}" if browser else ""
        raise ConfigError(f"No browser profiles with X session cookies were found{scope}.")

    table = Table(title="Browser profiles with X cookies", box=box.HORIZONTALS)
    table.add_column("#", no_wrap=True, style="cyan")
    table.add_column("Browser", style="green", no_wrap=True)
    table.add_column("Profile", no_wrap=True)
    table.add_column("Path", overflow="fold")
    table.add_column("Tags", no_wrap=True)
    for index, candidate in enumerate(candidates, start=1):
        table.add_row(
            str(index),
            candidate.browser_name,
            candidate.profile_name,
            str(candidate.profile_path),
            candidate.tags,
        )
    console.print(table)
    choice = Prompt.ask(
        "Choose browser profile",
        choices=[str(index) for index in range(1, len(candidates) + 1)],
        default="1",
        console=console,
    )
    return candidates[int(choice) - 1]


def _prepare_auth_override(
    config,
    console: Console,
    *,
    browser: str | None,
    profile: str | None,
    profile_path: Path | None,
    debug_auth: bool = False,
    interactive: bool = False,
):
    if interactive and (profile or (profile_path is not None)):
        raise ConfigError("--interactive cannot be combined with --profile or --profile-path.")
    if interactive:
        candidate = _pick_browser_candidate_interactively(console, browser=browser)
        browser = candidate.browser_id
        profile = None
        profile_path = candidate.profile_path

    if (profile or (profile_path is not None)) and not browser:
        raise ConfigError("--profile and --profile-path require --browser.")
    if not browser:
        return config, None

    pipeline = current_pipeline()
    auth_step_key = "auth-override"
    if pipeline is not None:
        profile_label = str(profile_path) if profile_path is not None else profile or "auto profile"
        pipeline.add_step(
            auth_step_key,
            "Authentication",
            total=1,
            unit="session",
            detail=f"{browser} · {profile_label}",
            show_rate=False,
            show_eta=False,
        )
        pipeline.start_step(
            auth_step_key,
            activity=f"Reading the X session from {browser}",
        )

    auth = config.auth.model_copy(
        update={
            "auth_token": None,
            "ct0": None,
            "browser": browser,
            "browser_profile": profile,
            "browser_profile_path": str(profile_path) if profile_path is not None else None,
            "firefox_profile_path": None,
        }
    )
    forced_config = config.model_copy(update={"auth": auth})
    auth_bundle = resolve_auth_bundle(
        forced_config,
        env=_browser_cookie_only_env(),
        status=_auth_status_callback(console, enabled=debug_auth),
    )
    if pipeline is not None:
        user_id = getattr(auth_bundle, "user_id", None)
        owner = f"X user {user_id}" if user_id else "session cookies"
        pipeline.complete_step(auth_step_key, f"{owner} resolved from {browser}")
    return forced_config, auth_bundle


def _normalize_collection_or_exit(collection: str, console: Console) -> str:
    try:
        return normalize_collection_name(collection)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _parse_search_types(value: str | None, console: Console) -> set[str] | None:
    if value is None:
        return None
    normalized: set[str] = set()
    invalid: list[str] = []
    for raw_part in value.split(","):
        part = raw_part.strip().lower()
        if not part:
            continue
        search_type = SEARCH_TYPE_ALIASES.get(part)
        if search_type is None:
            invalid.append(raw_part.strip())
            continue
        normalized.add(search_type)
    if invalid:
        allowed = ", ".join(sorted(SEARCH_TYPE_ALIASES))
        console.print(
            f"[red]Unsupported search type(s): {', '.join(invalid)}. "
            f"Expected one of: {allowed}.[/red]"
        )
        raise typer.Exit(1)
    return normalized or None


def _parse_search_collections(value: str | None, console: Console) -> set[str] | None:
    if value is None:
        return None
    normalized: set[str] = set()
    invalid: list[str] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            normalized.add(normalize_collection_name(part))
        except ValueError:
            invalid.append(part)
    if invalid:
        allowed = ", ".join(sorted({"bookmark", "bookmarks", "like", "likes", "tweet", "tweets"}))
        console.print(
            f"[red]Unsupported collection(s): {', '.join(invalid)}. "
            f"Expected one of: {allowed}.[/red]"
        )
        raise typer.Exit(1)
    return normalized or None


def _open_store_for_read(console: Console):
    from tweetxvault.reminders import print_archive_migration_report

    config, paths = load_config()
    store = open_archive_store(paths, create=False, config=config)
    if store is None:
        console.print("[red]No local archive found.[/red]")
        raise typer.Exit(1)
    print_archive_migration_report(console, store)
    return store, paths


def _with_archive_write_lock(paths, fn):
    lock = ProcessLock(paths.lock_file)
    lock.acquire()
    try:
        return fn()
    finally:
        lock.release()


def _with_auto_optimize(store, paths, console: Console, fn):
    """Run a storage operation through the shared read/write wrapper."""
    return fn(store)


def _archive_followup_summary(result: Any) -> str:
    reconciled = ", ".join(getattr(result, "reconciled_collections", [])) or "none"
    pending_untouched = getattr(result, "pending_untouched", result.pending_enrichment)
    transient_due = getattr(result, "transient_due", 0)
    transient_delayed = getattr(result, "transient_delayed", 0)
    return (
        f"live reconciliation: {reconciled}. detail enrichment: "
        f"{result.detail_lookups:,} refreshed, "
        f"{result.detail_terminal_unavailable:,} terminal, "
        f"{result.detail_transient_failures:,} transient failures, "
        f"{pending_untouched:,} pending untouched, "
        f"{transient_due:,} transient due, "
        f"{transient_delayed:,} transient delayed"
    )


def _archive_import_summary(result: Any) -> str:
    counts = result.counts
    prefix = "archive import"
    if result.skipped and result.followup_performed:
        prefix = (
            "archive import: already present; keeping existing imported data and running "
            "follow-up enrichment. Stored totals"
        )
    elif result.skipped:
        prefix = "archive import skipped: already imported. Stored totals"
    return (
        f"{prefix}: {counts.get('authored_tweets', 0)} authored, "
        f"{counts.get('deleted_authored_tweets', 0)} deleted authored, "
        f"{counts.get('likes', 0)} likes, "
        f"{counts.get('media_files_copied', 0)} media files copied. "
        f"{_archive_followup_summary(result)}"
    )


def _run_sync_command(
    *,
    browser: str | None,
    profile: str | None,
    profile_path: Path | None,
    runner: Callable[[Any, Any, Console], Awaitable[Any]],
    after: Callable[[Console], None] | None = None,
    summarize: Callable[[Any], str] | None = None,
) -> tuple[Console, Any]:
    console = _configure_logging()
    try:
        with PipelineReporter(console, "tweetxvault sync") as pipeline:
            config, _ = load_config()
            config, auth_bundle = _prepare_auth_override(
                config,
                console,
                browser=browser,
                profile=profile,
                profile_path=profile_path,
            )
            result = asyncio.run(runner(config, auth_bundle, console))
            if after is not None:
                after(console)
            errors = getattr(result, "errors", None)
            if errors:
                for name, error in errors.items():
                    pipeline.issue(
                        f"{str(name).title()} sync failed: {error}",
                        level="error",
                        dedupe_key=f"sync:{name}:failure",
                    )
                failed = ", ".join(str(name) for name in errors)
                completed = summarize(result) if summarize is not None else ""
                summary = f"{completed}. " if completed else ""
                pipeline.finish(
                    summary + f"Sync stopped after failure in {failed}.",
                    success=False,
                )
            elif (
                summarize is not None
                and not any(step.key.startswith(("preflight:", "sync:")) for step in pipeline.steps)
                and not pipeline.has_final_note
            ):
                pipeline.final_note(summarize(result))
        return console, result
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


def _sync_followup_plan(
    *,
    skip_resurrection: bool,
    skip_articles: bool,
    skip_media: bool,
    skip_unfurl: bool,
    skip_threads: bool,
) -> SyncFollowupPlan:
    return SyncFollowupPlan(
        resurrection=not skip_resurrection,
        articles=not skip_articles,
        media=not skip_media,
        unfurl=not skip_unfurl,
        threads=not skip_threads,
    )


def _run_sync_all_command(
    *,
    full: bool,
    backfill: bool,
    article_backfill: bool,
    head_only: bool,
    limit: int | None,
    browser: str | None,
    profile: str | None,
    profile_path: Path | None,
    skip_resurrection: bool,
    skip_articles: bool,
    skip_media: bool,
    skip_unfurl: bool,
    skip_threads: bool,
    max_linked_depth: int | None = None,
) -> None:
    followups = _sync_followup_plan(
        skip_resurrection=skip_resurrection,
        skip_articles=skip_articles,
        skip_media=skip_media,
        skip_unfurl=skip_unfurl,
        skip_threads=skip_threads,
    )
    console, outcome = _run_sync_command(
        browser=browser,
        profile=profile,
        profile_path=profile_path,
        runner=lambda config, auth_bundle, runner_console: _run_with_depth(
            config,
            auth_bundle,
            runner_console,
            max_linked_depth,
            full,
            backfill,
            article_backfill,
            head_only,
            limit,
            followups,
        ),
        after=_maybe_restart_web,
        summarize=lambda outcome: "; ".join(
            f"{item.collection}: {item.pages_fetched} pages, {item.tweets_seen} tweets"
            for item in outcome.results
        ),
    )
    raise typer.Exit(outcome.exit_code)


def _run_with_depth(
    config,
    auth_bundle,
    runner_console,
    max_linked_depth,
    full,
    backfill,
    article_backfill,
    head_only,
    limit,
    followups,
):
    if max_linked_depth is not None:
        config.sync.max_linked_depth = max_linked_depth
    return sync_all(
        full=full,
        backfill=backfill,
        article_backfill=article_backfill,
        head_only=head_only,
        limit=limit,
        config=config,
        auth_bundle=auth_bundle,
        console=runner_console,
        followups=followups,
    )


@sync_app.callback()
def sync_default(
    ctx: typer.Context,
    full: Annotated[bool, typer.Option("--full", help=SYNC_FULL_HELP)] = False,
    backfill: Annotated[
        bool,
        typer.Option("--backfill", help=SYNC_BACKFILL_HELP),
    ] = False,
    article_backfill: SYNC_ARTICLE_BACKFILL_OPTION = False,
    head_only: SYNC_HEAD_ONLY_OPTION = False,
    limit: Annotated[int | None, typer.Option("--limit", help=SYNC_LIMIT_HELP)] = None,
    browser: SYNC_BROWSER_OPTION = None,
    profile: SYNC_PROFILE_OPTION = None,
    profile_path: SYNC_PROFILE_PATH_OPTION = None,
    skip_resurrection: SYNC_SKIP_RESURRECTION_OPTION = False,
    skip_articles: SYNC_SKIP_ARTICLES_OPTION = False,
    skip_media: SYNC_SKIP_MEDIA_OPTION = False,
    skip_unfurl: SYNC_SKIP_UNFURL_OPTION = False,
    skip_threads: SYNC_SKIP_THREADS_OPTION = False,
    max_linked_depth: SYNC_MAX_LINKED_DEPTH_OPTION = None,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _run_sync_all_command(
        full=full,
        backfill=backfill,
        article_backfill=article_backfill,
        head_only=head_only,
        limit=limit,
        browser=browser,
        profile=profile,
        profile_path=profile_path,
        skip_resurrection=skip_resurrection,
        skip_articles=skip_articles,
        skip_media=skip_media,
        skip_unfurl=skip_unfurl,
        skip_threads=skip_threads,
        max_linked_depth=max_linked_depth,
    )


def _register_sync_collection_command(collection: str):
    command_help = {
        "bookmarks": "Sync bookmarked tweets.",
        "likes": "Sync liked tweets.",
        "tweets": "Sync authored tweets.",
    }[collection]

    def command(
        full: Annotated[bool, typer.Option("--full", help=SYNC_FULL_HELP)] = False,
        backfill: Annotated[
            bool,
            typer.Option("--backfill", help=SYNC_BACKFILL_HELP),
        ] = False,
        article_backfill: SYNC_ARTICLE_BACKFILL_OPTION = False,
        head_only: SYNC_HEAD_ONLY_OPTION = False,
        limit: Annotated[int | None, typer.Option("--limit", help=SYNC_LIMIT_HELP)] = None,
        browser: SYNC_BROWSER_OPTION = None,
        profile: SYNC_PROFILE_OPTION = None,
        profile_path: SYNC_PROFILE_PATH_OPTION = None,
        skip_resurrection: SYNC_SKIP_RESURRECTION_OPTION = False,
        skip_articles: SYNC_SKIP_ARTICLES_OPTION = False,
        skip_media: SYNC_SKIP_MEDIA_OPTION = False,
        skip_unfurl: SYNC_SKIP_UNFURL_OPTION = False,
        skip_threads: SYNC_SKIP_THREADS_OPTION = False,
    ) -> None:
        followups = _sync_followup_plan(
            skip_resurrection=skip_resurrection,
            skip_articles=skip_articles,
            skip_media=skip_media,
            skip_unfurl=skip_unfurl,
            skip_threads=skip_threads,
        )
        console, result = _run_sync_command(
            browser=browser,
            profile=profile,
            profile_path=profile_path,
            runner=lambda config, auth_bundle, runner_console: sync_collection(
                collection,
                full=full,
                backfill=backfill,
                article_backfill=article_backfill,
                head_only=head_only,
                limit=limit,
                config=config,
                auth_bundle=auth_bundle,
                console=runner_console,
                followups=followups,
            ),
            summarize=lambda result: (
                f"{collection}: {result.pages_fetched} pages, "
                f"{result.tweets_seen} tweets, {result.stop_reason}"
            ),
        )

    command.__name__ = f"sync_{collection}"
    return sync_app.command(collection, help=command_help)(command)


def _format_created_at(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        local_dt = dt.astimezone()
        date_part = local_dt.strftime("%b %-d, %Y")
        time_part = local_dt.strftime("%-I:%M %p").lower()
        return f"{date_part}\n{time_part}"
    except (ValueError, TypeError):
        return raw


def _format_stats_timestamp(raw: str | None) -> str:
    if not raw:
        return "-"
    parsed = _parse_created_at(raw)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return raw
    local_dt = parsed.astimezone() if parsed.tzinfo is not None else parsed
    date_part = local_dt.strftime("%b %-d, %Y")
    time_part = local_dt.strftime("%-I:%M %p").lower()
    return f"{date_part} {time_part}"


def _format_byte_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024.0
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
    return f"{size_bytes} B"


def _path_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _format_backfill_status(backfill_cursor: str | None, backfill_incomplete: bool) -> str:
    if backfill_incomplete and backfill_cursor:
        return "resume older"
    if backfill_incomplete:
        return "incomplete"
    if backfill_cursor:
        return "saved only"
    return "none saved"


def _format_optimize_status(version_count: int) -> str:
    if version_count >= 4:
        return "run optimize"
    return "ok"


def _parse_created_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        return None


def _search_result_score(row: dict[str, Any]) -> float:
    score = row.get("match_score")
    try:
        return float(score)
    except (TypeError, ValueError):
        return float("-inf")


def _sort_search_results(rows: list[dict[str, Any]], *, sort: str) -> list[dict[str, Any]]:
    if sort == "relevance":
        return rows

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        created_at = _parse_created_at(row.get("created_at"))
        score_key = -_search_result_score(row)
        tweet_id = str(row.get("tweet_id") or "")
        if created_at is None:
            return (1, 0.0, score_key, tweet_id)
        timestamp = created_at.timestamp()
        if sort == "oldest":
            return (0, timestamp, score_key, tweet_id)
        return (0, -timestamp, score_key, tweet_id)

    return sorted(rows, key=sort_key)


@dataclass(slots=True)
class _TweetListRow:
    tweet_id: str | None
    created_at: str | None
    author_username: str | None
    author_id: str | None
    text: Text
    match: str | None = None
    score: str | None = None


def _format_tweet_text(raw: str | None, *, highlight_query: str | None = None) -> Text:
    text = (raw or "").replace("\n", " ")
    rendered = _highlight_search_matches(text, highlight_query) if highlight_query else Text(text)
    if len(rendered.plain) > 280:
        rendered.truncate(280, overflow="ellipsis")
    return rendered


def _tweet_row_url(row: _TweetListRow) -> str:
    if row.author_username and row.tweet_id:
        return f"https://x.com/{row.author_username}/status/{row.tweet_id}"
    return f"https://x.com/i/web/status/{row.tweet_id or ''}"


def _render_tweet_list(
    console: Console,
    *,
    title: str,
    rows: list[_TweetListRow],
    count_line: str | None = None,
) -> None:
    include_match = any(row.match is not None for row in rows)
    table = Table(
        title=title,
        box=box.HORIZONTALS,
        show_lines=True,
    )
    if include_match:
        table.add_column("Match", style="yellow", no_wrap=True)
    table.add_column("Created", style="cyan", no_wrap=True)
    table.add_column("Author", style="green", no_wrap=True)
    table.add_column("Text", overflow="fold")
    table.add_column("URL", style="magenta", overflow="fold")

    for row in rows:
        username = row.author_username or row.author_id or "unknown"
        values: list[Any] = []
        if include_match:
            values.append(row.match or (row.score or ""))
        values.extend(
            [
                _format_created_at(row.created_at),
                f"@{username}",
                row.text,
                _tweet_row_url(row),
            ]
        )
        table.add_row(*values)

    if count_line:
        console.print(count_line)
    console.print(table)


def _render_archive_view(
    console: Console, *, collection: str, limit: int, sort: str = "newest"
) -> None:
    normalized = _normalize_collection_or_exit(collection, console)
    store, paths = _open_store_for_read(console)
    try:
        total_rows = store.count_export_rows(normalized)
        rows = _with_auto_optimize(
            store,
            paths,
            console,
            lambda s: s.export_rows(
                normalized,
                sort=sort,
                limit=limit,
                include_raw_json=False,
            ),
        )
    finally:
        store.close()

    label = display_collection_name(normalized)
    if not rows:
        console.print(f"[yellow]No archived {label} rows found.[/yellow]")
        return

    display_rows = [
        _TweetListRow(
            tweet_id=row.get("tweet_id"),
            created_at=row.get("created_at"),
            author_username=(row.get("author") or {}).get("username"),
            author_id=(row.get("author") or {}).get("id"),
            text=_format_tweet_text(row.get("text")),
        )
        for row in rows
    ]
    _render_tweet_list(
        console,
        title=f"{label} archive",
        rows=display_rows,
        count_line=f"showing {len(rows)} of {total_rows} archived {label} tweets",
    )


def _highlight_search_matches(text: str, query: str) -> Text:
    rendered = Text(text)
    tokens = [token for token in dict.fromkeys(query.split()) if token]
    if not tokens:
        return rendered
    pattern = re.compile("|".join(re.escape(token) for token in tokens), re.IGNORECASE)
    for match in pattern.finditer(text):
        rendered.stylize("black on yellow", match.start(), match.end())
    return rendered


sync_bookmarks = _register_sync_collection_command("bookmarks")
sync_likes = _register_sync_collection_command("likes")
sync_tweets = _register_sync_collection_command("tweets")


@sync_app.command("all", help=SYNC_ALL_HELP)
def sync_everything(
    full: Annotated[bool, typer.Option("--full", help=SYNC_FULL_HELP)] = False,
    backfill: Annotated[
        bool,
        typer.Option("--backfill", help=SYNC_BACKFILL_HELP),
    ] = False,
    article_backfill: SYNC_ARTICLE_BACKFILL_OPTION = False,
    head_only: SYNC_HEAD_ONLY_OPTION = False,
    limit: Annotated[int | None, typer.Option("--limit", help=SYNC_LIMIT_HELP)] = None,
    browser: SYNC_BROWSER_OPTION = None,
    profile: SYNC_PROFILE_OPTION = None,
    profile_path: SYNC_PROFILE_PATH_OPTION = None,
    skip_resurrection: SYNC_SKIP_RESURRECTION_OPTION = False,
    skip_articles: SYNC_SKIP_ARTICLES_OPTION = False,
    skip_media: SYNC_SKIP_MEDIA_OPTION = False,
    skip_unfurl: SYNC_SKIP_UNFURL_OPTION = False,
    skip_threads: SYNC_SKIP_THREADS_OPTION = False,
    max_linked_depth: SYNC_MAX_LINKED_DEPTH_OPTION = None,
) -> None:
    _run_sync_all_command(
        full=full,
        backfill=backfill,
        article_backfill=article_backfill,
        head_only=head_only,
        limit=limit,
        browser=browser,
        profile=profile,
        profile_path=profile_path,
        skip_resurrection=skip_resurrection,
        skip_articles=skip_articles,
        skip_media=skip_media,
        skip_unfurl=skip_unfurl,
        skip_threads=skip_threads,
        max_linked_depth=max_linked_depth,
    )


@auth_app.command("check", help="Validate local auth and probe remote timeline readiness.")
def auth_check(
    browser: Annotated[str | None, typer.Option("--browser", help=BROWSER_HELP)] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Browser profile name or directory name."),
    ] = None,
    profile_path: Annotated[
        Path | None,
        typer.Option("--profile-path", help="Explicit browser profile directory path."),
    ] = None,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", help="Interactively choose a browser profile."),
    ] = False,
    debug_auth: DEBUG_AUTH_OPTION = False,
) -> None:
    console = _configure_logging()
    config, paths = load_config()
    try:
        config, auth_bundle = _prepare_auth_override(
            config,
            console,
            browser=browser,
            profile=profile,
            profile_path=profile_path,
            debug_auth=debug_auth,
            interactive=interactive,
        )
        if auth_bundle is None:
            auth_bundle = resolve_auth_bundle(
                config,
                status=_auth_status_callback(console, enabled=debug_auth),
            )
        result = asyncio.run(
            run_preflight(
                config=config,
                paths=paths,
                collections=["bookmarks", "likes", "tweets"],
                auth_bundle=auth_bundle,
            )
        )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    console.print(
        f"local auth: auth_token={result.auth.auth_token_source}, ct0={result.auth.ct0_source}, "
        f"user_id={result.auth.user_id_source or 'missing'}"
    )
    for collection, probe in result.probes.items():
        status = "ready" if probe.ready else "not ready"
        console.print(f"{collection}: {status} ({probe.detail})")
    if result.has_local_error:
        raise typer.Exit(1)
    if result.has_remote_error:
        raise typer.Exit(2)


@auth_app.command("refresh-ids", help="Force-refresh GraphQL query IDs from X's JS bundles.")
def auth_refresh_ids() -> None:
    console = _configure_logging()
    _, paths = load_config()
    ensure_paths(paths)
    store = QueryIdStore(paths)

    async def _refresh() -> None:
        await refresh_query_ids(store)

    try:
        asyncio.run(_refresh())
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    cache = store.load()
    console.print(f"refreshed {len(cache.ids)} query IDs into {store.path}")


@article_app.command("refresh", help="Refresh archived article bodies via TweetDetail.")
def refresh_archived_articles(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="Tweet IDs or x.com status URLs to refresh."),
    ] = None,
    all_articles: Annotated[
        bool,
        typer.Option(
            "--all", help="Refresh all archived article rows, not just preview-only ones."
        ),
    ] = False,
    limit: ARTICLE_LIMIT_OPTION = None,
    browser: Annotated[str | None, typer.Option("--browser", help=BROWSER_HELP)] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Browser profile name or directory name."),
    ] = None,
    profile_path: Annotated[
        Path | None,
        typer.Option("--profile-path", help="Explicit browser profile directory path."),
    ] = None,
) -> None:
    console = _configure_logging()
    try:
        with PipelineReporter(console, "tweetxvault articles refresh") as pipeline:
            if all_articles and targets:
                raise ConfigError("--all cannot be combined with explicit article targets.")
            config, paths = load_config()
            config, auth_bundle = _prepare_auth_override(
                config,
                console,
                browser=browser,
                profile=profile,
                profile_path=profile_path,
            )
            result = asyncio.run(
                refresh_articles(
                    targets=targets,
                    preview_only=not all_articles,
                    limit=limit,
                    config=config,
                    paths=paths,
                    auth_bundle=auth_bundle,
                    console=console,
                )
            )
            if not pipeline.has_step("articles"):
                prefix = "No article rows required refresh. " if result.processed == 0 else ""
                pipeline.final_note(
                    prefix + "articles: "
                    f"{result.processed} processed, "
                    f"{result.updated} refreshed, "
                    f"{result.failed} failed"
                )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@thread_app.command("expand", help="Expand archived tweet threads via TweetDetail.")
def expand_archive_threads(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="Tweet IDs or x.com status URLs to expand."),
    ] = None,
    limit: THREAD_LIMIT_OPTION = None,
    browser: Annotated[str | None, typer.Option("--browser", help=BROWSER_HELP)] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Browser profile name or directory name."),
    ] = None,
    profile_path: Annotated[
        Path | None,
        typer.Option("--profile-path", help="Explicit browser profile directory path."),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Re-fetch explicit thread targets even if they were already expanded.",
        ),
    ] = False,
    debug_auth: DEBUG_AUTH_OPTION = False,
    max_linked_depth: SYNC_MAX_LINKED_DEPTH_OPTION = None,
) -> None:
    console = _configure_logging()
    try:
        with PipelineReporter(console, "tweetxvault threads expand") as pipeline:
            config, paths = load_config()
            if max_linked_depth is not None:
                config.sync.max_linked_depth = max_linked_depth
            config, auth_bundle = _prepare_auth_override(
                config,
                console,
                browser=browser,
                profile=profile,
                profile_path=profile_path,
                debug_auth=debug_auth,
            )
            result = asyncio.run(
                expand_threads(
                    targets=targets,
                    limit=limit,
                    refresh=refresh,
                    config=config,
                    paths=paths,
                    auth_bundle=auth_bundle,
                    auth_status=_auth_status_callback(console, enabled=debug_auth),
                    console=console,
                )
            )
            if not pipeline.has_step("threads"):
                prefix = "No thread candidates required fetching. " if result.processed == 0 else ""
                pipeline.final_note(
                    prefix + "threads: "
                    f"{result.processed} processed, "
                    f"{result.expanded} expanded, "
                    f"{result.skipped} skipped, "
                    f"{result.failed} failed"
                )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@view_app.command("bookmarks", help="View bookmarked tweets.")
def view_bookmarks(limit: VIEW_LIMIT_OPTION = 20, sort: VIEW_SORT_OPTION = "newest") -> None:
    console = _configure_logging()
    _render_archive_view(console, collection="bookmarks", limit=limit, sort=sort)


@view_app.command("likes", help="View liked tweets.")
def view_likes(limit: VIEW_LIMIT_OPTION = 20, sort: VIEW_SORT_OPTION = "newest") -> None:
    console = _configure_logging()
    _render_archive_view(console, collection="likes", limit=limit, sort=sort)


@view_app.command("all", help="View bookmarks, likes, and authored tweets together.")
def view_all(limit: VIEW_LIMIT_OPTION = 20, sort: VIEW_SORT_OPTION = "newest") -> None:
    console = _configure_logging()
    _render_archive_view(console, collection="all", limit=limit, sort=sort)


@view_app.command("tweets", help="View authored tweets.")
def view_tweets(limit: VIEW_LIMIT_OPTION = 20, sort: VIEW_SORT_OPTION = "newest") -> None:
    console = _configure_logging()
    _render_archive_view(console, collection="tweets", limit=limit, sort=sort)


@export_app.command("json", help="Export the archive as JSON.")
def export_json(
    collection: EXPORT_COLLECTION_OPTION = "all",
    out: EXPORT_OUT_OPTION = None,
) -> None:
    console = _configure_logging()
    normalized = _normalize_collection_or_exit(collection, console)
    store, paths = _open_store_for_read(console)
    try:
        out_path = out or default_export_path(
            paths.data_dir / "exports",
            normalized,
            extension="json",
        )
        _with_auto_optimize(
            store,
            paths,
            console,
            lambda s: export_json_archive(s, collection=normalized, out_path=out_path),
        )
    finally:
        store.close()
    console.print(f"exported {display_collection_name(normalized)} archive to {out_path}")


@export_app.command("html", help="Export the archive as HTML.")
def export_html(
    collection: EXPORT_COLLECTION_OPTION = "all",
    out: EXPORT_OUT_OPTION = None,
) -> None:
    console = _configure_logging()
    normalized = _normalize_collection_or_exit(collection, console)
    store, paths = _open_store_for_read(console)
    try:
        out_path = out or default_export_path(
            paths.data_dir / "exports",
            normalized,
            extension="html",
        )
        _with_auto_optimize(
            store,
            paths,
            console,
            lambda s: export_html_archive(s, collection=normalized, out_path=out_path),
        )
    finally:
        store.close()
    console.print(f"exported {display_collection_name(normalized)} archive to {out_path}")


@import_app.command("grailbird")
def import_grailbird_command(
    input_dir: Annotated[Path, typer.Argument(help="Path to the old Grailbird archive directory.")],
    output_dir: Annotated[
        Path, typer.Argument(help="Path to the converted modern archive directory.")
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite the output directory if it already exists.",
        ),
    ] = False,
) -> None:
    console = _configure_logging()
    try:
        result = convert_grailbird_archive(input_dir, output_dir, force=force)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if result.screen_name:
        console.print(
            f"grailbird convert: {result.tweet_count} tweets for "
            f"@{result.screen_name} -> {result.output_path}",
            highlight=False,
        )
    else:
        console.print(
            f"grailbird convert: {result.tweet_count} tweets -> {result.output_path}",
            highlight=False,
        )
        console.print(
            "account metadata unavailable; the first authenticated sync/import follow-up can "
            "establish the archive owner later",
            highlight=False,
        )
    for warning in result.warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    console.print(f'tweetxvault import x-archive "{result.output_path}"', highlight=False)


@import_app.command("x-archive", help="Import an official X archive ZIP or extracted directory.")
def import_x_archive_command(
    archive: Annotated[
        Path, typer.Argument(help="Path to an X archive zip or extracted directory.")
    ],
    regen: Annotated[
        bool,
        typer.Option(
            "--regen",
            help=(
                "Clear archive-import-owned rows, manifests, and imported media files before "
                "reimporting. Live-synced rows are kept."
            ),
        ),
    ] = False,
    enrich: Annotated[
        bool,
        typer.Option(
            "--enrich/--no-enrich",
            help=(
                "Automatically complete sparse archive TweetDetail enrichment after import. "
                "Bulk live reconciliation still runs with --no-enrich."
            ),
        ),
    ] = True,
    detail_lookups: Annotated[
        int,
        typer.Option(
            "--detail-lookups",
            min=0,
            help=(
                "Bound automatic TweetDetail enrichment to this many sparse tweets for this "
                "import invocation."
            ),
        ),
    ] = 0,
    sample_limit: Annotated[
        int | None,
        typer.Option(
            "--sample-limit",
            min=1,
            help=(
                "Sample mode: import at most N authored tweets, deleted tweets, likes, and "
                "media files after full dataset load. This stores a sampled manifest instead "
                "of a completed import."
            ),
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help=(
                "Print detailed archive-import timing diagnostics. Interactive TTY runs already "
                "show the unified pipeline by default."
            ),
        ),
    ] = False,
    browser: SYNC_BROWSER_OPTION = None,
    profile: SYNC_PROFILE_OPTION = None,
    profile_path: SYNC_PROFILE_PATH_OPTION = None,
    debug_auth: DEBUG_AUTH_OPTION = False,
) -> None:
    console = _configure_logging()
    config = None
    paths = None
    try:
        with PipelineReporter(console, "tweetxvault import x-archive") as pipeline:
            config, paths = load_config()
            config, auth_bundle = _prepare_auth_override(
                config,
                console,
                browser=browser,
                profile=profile,
                profile_path=profile_path,
                debug_auth=debug_auth,
            )
            result = asyncio.run(
                import_x_archive(
                    archive,
                    regen=regen,
                    enrich=enrich,
                    detail_lookups=detail_lookups,
                    sample_limit=sample_limit,
                    debug=debug,
                    config=config,
                    paths=paths,
                    auth_bundle=auth_bundle,
                    console=console,
                )
            )
            if not (result.skipped and not result.followup_performed):
                _maybe_restart_web(console)
            if not pipeline.has_step("archive-inspect") and not pipeline.has_final_note:
                pipeline.final_note(_archive_import_summary(result))
                for warning in result.warnings:
                    pipeline.issue(
                        warning,
                        dedupe_key=f"archive-import:{warning.split(':', 1)[0]}",
                    )
    except ArchiveEnrichmentInterrupted as exc:
        console.print("Archive import is complete.", highlight=False)
        console.print("\nArchive enrichment was interrupted.", highlight=False)
        console.print(
            f"{exc.remaining:,} sparse archive tweets remain incomplete.",
            highlight=False,
        )
        console.print("\nContinue later with:\n  tweetxvault import enrich", highlight=False)
        raise typer.Exit(130) from exc
    except ArchiveEnrichmentAborted as exc:
        console.print("Archive import is complete.", highlight=False)
        console.print("\nAutomatic archive enrichment stopped:", highlight=False)
        console.print(f"[red]{exc.cause}[/red]")
        console.print(f"{exc.remaining:,} sparse archive tweets remain incomplete.")
        console.print("\nContinue later with:\n  tweetxvault import enrich", highlight=False)
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@import_app.command(
    "enrich",
    help="Process every archive TweetDetail row eligible when the command starts.",
)
def import_archive_enrich(
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help=(
                "Maximum sparse tweets to process in this continuation run. "
                "Omit to process every currently eligible row."
            ),
        ),
    ] = None,
    browser: SYNC_BROWSER_OPTION = None,
    profile: SYNC_PROFILE_OPTION = None,
    profile_path: SYNC_PROFILE_PATH_OPTION = None,
    debug_auth: DEBUG_AUTH_OPTION = False,
) -> None:
    console = _configure_logging()
    try:
        with PipelineReporter(console, "tweetxvault import enrich") as pipeline:
            config, paths = load_config()
            config, auth_bundle = _prepare_auth_override(
                config,
                console,
                browser=browser,
                profile=profile,
                profile_path=profile_path,
                debug_auth=debug_auth,
            )
            result = asyncio.run(
                enrich_imported_archive(
                    limit=limit,
                    config=config,
                    paths=paths,
                    auth_bundle=auth_bundle,
                    console=console,
                )
            )
            if not pipeline.has_step("archive-enrich") and not pipeline.has_final_note:
                pipeline.final_note(
                    "archive enrich: existing imported archive data. "
                    + _archive_followup_summary(result)
                )
                for warning in result.warnings:
                    pipeline.issue(
                        warning,
                        dedupe_key=f"archive-enrich:{warning.split(':', 1)[0]}",
                    )
    except ArchiveEnrichmentInterrupted as exc:
        console.print("Archive enrichment was interrupted.", highlight=False)
        console.print(f"{exc.remaining:,} tweets remain incomplete.", highlight=False)
        console.print("Run `tweetxvault import enrich` to continue.", highlight=False)
        raise typer.Exit(130) from exc
    except ArchiveEnrichmentAborted as exc:
        console.print(f"[red]{exc}[/red]")
        console.print(f"{exc.remaining:,} tweets remain incomplete.", highlight=False)
        console.print("Run `tweetxvault import enrich` to continue.", highlight=False)
        raise typer.Exit(2) from exc
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@repair_app.command(
    "legacy-tombstones",
    help="Recover richer content for legacy unavailable tweet tombstones.",
)
def repair_legacy_tombstones(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report recoverable legacy rows without modifying the archive.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help="Maximum number of suspicious legacy tombstone rows to inspect.",
        ),
    ] = None,
    scan_timeline_captures: Annotated[
        bool,
        typer.Option(
            "--scan-timeline-captures",
            help="Also scan large timeline captures for richer recovery candidates.",
        ),
    ] = False,
) -> None:
    from tweetxvault.reminders import print_archive_migration_report

    console = _configure_logging()
    config, paths = load_config()
    lock = ProcessLock(paths.lock_file)
    lock.acquire()
    store = None
    try:
        store = open_archive_store(paths, create=False, config=config)
        if store is None:
            raise ConfigError("No local archive found.")
        print_archive_migration_report(console, store)
        report = store.repair_legacy_terminal_rows(
            limit=limit,
            scan_timeline_captures=scan_timeline_captures,
            dry_run=dry_run,
        )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        if store is not None:
            store.close()
        lock.release()

    prefix = "legacy tombstone dry run" if dry_run else "legacy tombstone repair"
    content_label = "recoverable" if dry_run else "repaired"
    author_label = "recoverable" if dry_run else "restored"
    console.print(
        f"{prefix}: {report['legacy_terminal_rows_scanned']:,} scanned, "
        f"{report['content_rows_repaired']:,} content rows {content_label}, "
        f"{report['author_rows_repaired']:,} authors {author_label}, "
        f"{report['rows_still_missing_author']:,} still missing authors, "
        f"{report['rows_without_richer_source']:,} without a richer local source",
        highlight=False,
    )


@media_app.command("download", help="Download archived tweet media files.")
def media_download(
    limit: MEDIA_LIMIT_OPTION = None,
    photos_only: PHOTOS_ONLY_OPTION = False,
    retry_failed: RETRY_FAILED_OPTION = False,
) -> None:
    console = _configure_logging()
    try:
        with PipelineReporter(console, "tweetxvault media download") as pipeline:
            config, paths = load_config()
            result = asyncio.run(
                download_media(
                    limit=limit,
                    photos_only=photos_only,
                    retry_failed=retry_failed,
                    config=config,
                    paths=paths,
                    console=console,
                )
            )
            if not pipeline.steps:
                prefix = "No media files require download. " if result.processed == 0 else ""
                pipeline.final_note(
                    prefix + "media: "
                    f"{result.processed} processed, "
                    f"{result.downloaded} downloaded, "
                    f"{result.skipped} skipped, "
                    f"{result.failed} failed"
                )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@app.command("unfurl", help="Fetch canonical URL metadata for saved links.")
def unfurl_archive(
    limit: UNFURL_LIMIT_OPTION = None,
    retry_failed: RETRY_FAILED_OPTION = False,
) -> None:
    console = _configure_logging()
    try:
        with PipelineReporter(console, "tweetxvault unfurl") as pipeline:
            config, paths = load_config()
            result = asyncio.run(
                unfurl_urls(
                    limit=limit,
                    retry_failed=retry_failed,
                    config=config,
                    paths=paths,
                    console=console,
                )
            )
            if not pipeline.steps:
                prefix = "No saved URLs require metadata. " if result.processed == 0 else ""
                pipeline.final_note(
                    prefix + f"unfurl: {result.processed} processed, "
                    f"{result.updated} updated, {result.failed} failed"
                )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@app.command("tag", help="Use Gemini to generate search tags and descriptions for media tweets.")
def tag_archive(
    target: Annotated[
        str | None,
        typer.Argument(help="Tweet ID or x.com status URL to tag."),
    ] = None,
    limit: TAG_LIMIT_OPTION = None,
    test: Annotated[
        bool,
        typer.Option(
            "--test",
            help="Generate and display tags for one tweet without saving media tags.",
        ),
    ] = False,
    batch: Annotated[
        bool,
        typer.Option(
            "--batch",
            help="Batch tweets even when batching is disabled in config.toml.",
        ),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the Gemini model specified in config.toml"),
    ] = None,
) -> None:
    console = _configure_logging()
    try:
        config, paths = load_config()
        from tweetxvault.jobs import locked_archive_job
        from tweetxvault.tagging import (
            TaggingRunResult,
            tag_media_tweets,
            tag_pending_media_tweets,
        )

        tweet_id = None
        if target is not None:
            candidate = target.strip()
            tweet_id = candidate if candidate.isdigit() else extract_status_id_from_url(candidate)
            if tweet_id is None:
                raise ConfigError("Unsupported tag target. Use a tweet ID or x.com status URL.")

        async def run_tagging() -> TaggingRunResult:
            async with locked_archive_job(config=config, paths=paths, console=console) as job:
                if tweet_id is not None:
                    pipeline = current_pipeline()
                    if pipeline is not None:
                        pipeline.add_step(
                            "tagging",
                            "Tagging",
                            total=1,
                            unit="tweet",
                            detail=f"explicit target · {model or config.tagging.model}",
                            show_eta=False,
                        )
                        pipeline.start_step(
                            "tagging",
                            activity=f"Generating media tags for tweet {tweet_id}",
                            counters="0 processed · 0 tagged",
                        )
                    tagged = await tag_media_tweets(
                        store=job.store,
                        config=config,
                        paths=paths,
                        console=console,
                        tweet_ids=[tweet_id],
                        model_override=model,
                        dry_run=test,
                    )
                    if pipeline is not None:
                        summary = f"1 processed · {tagged} tagged" + (
                            " · test mode, not saved" if test else ""
                        )
                        pipeline.complete_step(
                            "tagging",
                            summary,
                            counters=summary,
                        )
                    return TaggingRunResult(processed=1, tagged=tagged, batches=1)

                return await tag_pending_media_tweets(
                    store=job.store,
                    config=config,
                    paths=paths,
                    console=console,
                    limit=limit,
                    batch_override=batch,
                    model_override=model,
                    dry_run=test,
                )

        with PipelineReporter(console, "tweetxvault tag") as pipeline:
            result = asyncio.run(run_tagging())
            if not pipeline.steps:
                if result.processed == 0:
                    pipeline.final_note("No eligible untagged media tweets found.")
                elif not test:
                    pipeline.final_note(
                        f"tag: {result.processed} processed, {result.tagged} tagged"
                    )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except TweetXVaultError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@app.command("optimize")
def optimize_archive() -> None:
    """Vacuum the SQLite database to reclaim space after large deletions."""
    console = _configure_logging()
    config, paths = load_config()

    def run() -> None:
        store = open_archive_store(paths, create=False, config=config)
        if store is None:
            console.print("[red]No local archive found.[/red]")
            raise typer.Exit(1)
        try:
            console.print("vacuuming database...")
            store.optimize()
            console.print("vacuum complete.")
        finally:
            store.close()

    try:
        _with_archive_write_lock(paths, run)
    except ProcessLockError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@db_app.command("check", help="Run an explicit SQLite database integrity check.")
def check_database(
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Run SQLite integrity_check instead of the faster quick_check.",
        ),
    ] = False,
) -> None:
    console = _configure_logging()
    store, paths = _open_store_for_read(console)
    pragma = "integrity_check" if full else "quick_check"
    try:
        results = store.check_integrity(full=full)
    except Exception as exc:
        console.print(f"[red]database {pragma} failed: {exc}[/red]")
        raise typer.Exit(2) from exc
    finally:
        store.close()

    if results == ["ok"]:
        console.print(f"database {pragma}: ok ({paths.database_path})", highlight=False)
        return

    console.print(f"[red]database {pragma} reported problems:[/red]")
    for result in results:
        console.print(result, highlight=False)
    raise typer.Exit(2)


@app.command("stats")
def stats_archive() -> None:
    """Show archive totals, collection coverage, sync timestamps, and storage health."""
    console = _configure_logging()
    store, paths = _open_store_for_read(console)
    try:
        stats = store.archive_stats()
        db_size = _path_size_bytes(paths.database_path)
        media_size = _path_size_bytes(paths.media_dir)

        console.print(f"archive: {paths.database_path}", highlight=False)

        summary = Table(title="Summary", box=box.HORIZONTALS)
        summary.add_column("Metric", style="cyan", no_wrap=True)
        summary.add_column("Value", overflow="fold")
        summary.add_row("Owner", stats.owner_user_id or "unknown")
        summary.add_row("Unique posts", str(stats.unique_post_count))
        summary.add_row("Articles", str(stats.article_count))
        summary.add_row("Collection memberships", str(stats.collection_membership_count))
        summary.add_row("Raw captures", str(stats.raw_capture_count))
        summary.add_row("Media rows", str(stats.media_count))
        summary.add_row("URL rows", str(stats.url_count))
        summary.add_row("First post", _format_stats_timestamp(stats.oldest_created_at))
        summary.add_row("Latest post", _format_stats_timestamp(stats.newest_created_at))
        summary.add_row("Latest capture", _format_stats_timestamp(stats.latest_capture_at))
        summary.add_row("Last sync", _format_stats_timestamp(stats.latest_sync_at))
        console.print(summary)

        collections = Table(title="Collections", box=box.HORIZONTALS)
        collections.add_column("Collection", style="green", no_wrap=True)
        collections.add_column("Posts", justify="right", no_wrap=True)
        collections.add_column("First", no_wrap=True)
        collections.add_column("Last", no_wrap=True)
        collections.add_column("Last sync", no_wrap=True)
        collections.add_column("Backfill", no_wrap=True)
        for collection in stats.collections:
            collections.add_row(
                collection.collection_type,
                str(collection.post_count),
                _format_stats_timestamp(collection.oldest_created_at),
                _format_stats_timestamp(collection.newest_created_at),
                _format_stats_timestamp(collection.last_synced_at),
                _format_backfill_status(
                    collection.backfill_cursor,
                    collection.backfill_incomplete,
                ),
            )
        console.print(collections)

        storage = Table(title="Storage", box=box.HORIZONTALS)
        storage.add_column("Metric", style="cyan", no_wrap=True)
        storage.add_column("Value", overflow="fold")
        storage.add_row("DB size", _format_byte_size(db_size))
        storage.add_row("Media size", _format_byte_size(media_size))
        storage.add_row("Versions", str(stats.version_count))
        storage.add_row("Optimize", _format_optimize_status(stats.version_count))
        console.print(storage)

        followup = Table(title="Follow-Up", box=box.HORIZONTALS)
        followup.add_column("Task", style="cyan", no_wrap=True)
        followup.add_column("Status", overflow="fold")
        followup.add_row(
            "Archive enrich (TweetDetail)",
            (
                f"{stats.pending_enrichment_count} pending, "
                f"{getattr(stats, 'transient_enrichment_due_count', 0)} transient due, "
                f"{getattr(stats, 'transient_enrichment_delayed_count', 0)} transient delayed, "
                f"{getattr(stats, 'retryable_unavailable_count', stats.terminal_enrichment_count)} "
                "retryable unavailable, "
                f"{getattr(stats, 'permanent_unavailable_count', 0)} permanent unavailable, "
                f"{stats.resurrected_enrichment_count} resurrected, "
                f"{stats.done_enrichment_count} done"
            ),
        )
        followup.add_row(
            "Tweet resurrection",
            f"{getattr(stats, 'due_resurrection_count', 0)} unavailable tweets currently due "
            "for checking",
        )
        followup.add_row(
            "Articles refresh",
            f"{stats.preview_article_count} preview-only article rows",
        )
        followup.add_row(
            "Rehydrate gaps (local rebuild)",
            f"{stats.missing_tweet_object_count} tweets missing normalized tweet_object rows",
        )
        followup.add_row(
            "Threads expand (TweetDetail)",
            (
                f"{stats.expanded_thread_target_count} expanded, "
                f"{stats.pending_thread_membership_count} membership targets pending, "
                f"{stats.pending_thread_linked_status_count} linked-status targets pending"
            ),
        )
        console.print(followup)

        legend = Table(title="Legend", box=box.HORIZONTALS)
        legend.add_column("Label", style="cyan", no_wrap=True)
        legend.add_column("Meaning", overflow="fold")
        legend.add_row(
            "Backfill",
            (
                "'resume older' means the next sync will do its normal head pass, then "
                "resume older history from a saved cursor. 'none saved' means no older-"
                "history cursor is saved. 'saved only' and 'incomplete' are unusual "
                "transition states."
            ),
        )
        legend.add_row(
            "Archive enrich",
            (
                "Sparse archive-imported tweets still waiting for network TweetDetail "
                "lookups from X. Retryable failures can succeed later; terminal ones are "
                "known unavailable."
            ),
        )
        legend.add_row(
            "Articles refresh",
            (
                "Article rows that only have preview metadata. "
                "'tweetxvault articles refresh' can fetch the full body later."
            ),
        )
        legend.add_row(
            "Rehydrate gaps",
            (
                "Stored raw tweet JSON exists locally, but the normalized tweet_object row "
                "is missing. 'tweetxvault rehydrate' can rebuild these without a network call."
            ),
        )
        legend.add_row(
            "Threads expand",
            (
                "'membership targets' are archived bookmark/like/tweet post ids that have "
                "not been expanded through TweetDetail yet. 'linked-status targets' are "
                "extra post ids discovered inside saved x.com status URLs."
            ),
        )
        console.print(legend)
        print_pending_archive_enrichment_reminder(console, store)
    finally:
        store.close()


@app.command("rehydrate")
def rehydrate_archive() -> None:
    """Rebuild normalized tweet fields and secondary rows from stored raw_json."""
    from tqdm import tqdm

    console = _configure_logging()
    config, paths = load_config()

    def run() -> None:
        store = open_archive_store(paths, create=False, config=config)
        if store is None:
            console.print("[red]No local archive found.[/red]")
            raise typer.Exit(1)
        try:
            total = store._count("record_type = 'tweet'")
            if total == 0:
                console.print("archive has no tweet rows")
                return
            with tqdm(total=total, desc="rehydrating", unit="tweets") as pbar:
                result = store.rehydrate_from_raw_json(progress=pbar.update)
            if result.tweets_updated or result.secondary_records:
                pass
            console.print(
                f"rehydrated {result.tweets_updated} tweet rows and rebuilt "
                f"{result.secondary_records} secondary rows"
            )
        finally:
            store.close()

    try:
        _with_archive_write_lock(paths, run)
    except ProcessLockError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


@app.command("search")
def search_archive(
    query: SEARCH_QUERY_ARGUMENT,
    limit: SEARCH_LIMIT_OPTION = 20,
    sort: SEARCH_SORT_OPTION = "relevance",
    type_filter: Annotated[str | None, typer.Option("--type", help=SEARCH_TYPE_HELP)] = None,
    collection_filter: Annotated[
        str | None, typer.Option("--collection", help=SEARCH_COLLECTION_HELP)
    ] = None,
) -> None:
    """Full-text search archived posts and articles."""
    console = _configure_logging()
    store, paths = _open_store_for_read(console)
    try:
        search_types = _parse_search_types(type_filter, console)
        search_collections = _parse_search_collections(collection_filter, console)
        results = _with_auto_optimize(
            store,
            paths,
            console,
            lambda s: s.search_fts(
                query,
                limit=limit,
                types=search_types,
                collections=search_collections,
            ),
        )

        results = _sort_search_results(results, sort=sort)
        if not results:
            console.print("[yellow]No results found.[/yellow]")
            return

        display_rows: list[_TweetListRow] = []
        for row in results:
            score = row.get("match_score")
            if isinstance(score, float):
                score = f"{score:.3f}"
            type_label = row.get("type") or "post"
            collections = row.get("collections") or []
            match_label = str(type_label)
            if collections:
                match_label = f"{match_label} · {','.join(str(item) for item in collections)}"
            if score not in (None, ""):
                match_label = f"{match_label}\n{score}"
            display_rows.append(
                _TweetListRow(
                    tweet_id=row.get("tweet_id"),
                    created_at=row.get("created_at"),
                    author_username=row.get("author_username"),
                    author_id=row.get("author_id"),
                    text=_format_tweet_text(row.get("text"), highlight_query=query),
                    match=match_label,
                    score=str(score) if score not in (None, "") else None,
                )
            )
        _render_tweet_list(
            console,
            title=f"search: {query}",
            rows=display_rows,
            count_line=f"showing {len(display_rows)} search results",
        )
    finally:
        store.close()


@app.command("serve-daemon", hidden=True)
def serve_daemon_internal() -> None:
    """Internal command used by 'web start' to run the server process."""
    try:
        from tweetxvault.web.server import run_server
    except ImportError as exc:
        raise typer.Exit(1) from exc

    config, paths = load_config()

    store = open_archive_store(paths, create=False, config=config)
    if store is None:
        raise typer.Exit(1)
    store.close()

    web = config.web
    run_server(config, paths, web.host, web.port, web.password_hash)


def _maybe_restart_web(console: Console) -> None:
    """Restart the web server after sync if auto_start is enabled."""
    try:
        config, paths = load_config()
    except Exception:
        return
    if not config.web.auto_start:
        return
    pipeline = current_pipeline()
    step_key = "web-restart"
    if pipeline is not None:
        pipeline.add_step(
            step_key,
            "Web server",
            total=1,
            unit="restart",
            detail=f"auto-start enabled · http://{config.web.host}:{config.web.port}",
            show_rate=False,
            show_eta=False,
        )
        pipeline.start_step(
            step_key,
            activity="Restarting the local archive web server",
        )
    try:
        from tweetxvault.cli_web import _get_pid_file, _is_running
    except ImportError as exc:
        if pipeline is not None:
            message = "Web auto-start is enabled, but Web dependencies are unavailable."
            pipeline.fail_step(step_key, message)
            pipeline.issue(
                f"{message} {exc}",
                dedupe_key="web-restart:missing-dependency",
            )
        else:
            console.print(
                "[yellow]Web auto-start is enabled, but Web dependencies are unavailable.[/yellow]"
            )
        return

    import os
    import signal

    pid_file = _get_pid_file(paths.data_dir)
    # Stop existing server if running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _is_running(pid):
                os.kill(pid, signal.SIGTERM)
                if pipeline is not None:
                    pipeline.status(step_key, "Stopping the existing web server")
                else:
                    console.print("[dim]Stopping web server for restart...[/dim]")
                import time

                for _ in range(50):
                    if not _is_running(pid):
                        break
                    time.sleep(0.1)
                else:
                    # Still running after 5 seconds, force kill
                    try:
                        os.kill(pid, signal.SIGKILL)
                        time.sleep(0.5)
                    except OSError:
                        pass
        except (ValueError, ProcessLookupError):
            pass
        finally:
            if pid_file.exists():
                pid_file.unlink()

    # Start fresh
    cmd = [sys.executable, "-m", "tweetxvault", "serve-daemon"]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file.write_text(str(process.pid))
    web = config.web
    if pipeline is not None:
        pipeline.complete_step(
            step_key,
            f"restarted on http://{web.host}:{web.port} · PID {process.pid}",
        )
    else:
        console.print(
            f"[green]Web server restarted on http://{web.host}:{web.port} "
            f"(PID: {process.pid})[/green]"
        )


@app.command()
def migrate() -> None:
    """Migrate data from older LanceDB storage to native SQLite storage."""
    from tweetxvault.storage.migrate import run_migration

    run_migration()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """tweetxvault CLI."""
    del version
