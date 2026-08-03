"""Thread/context expansion helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from rich.console import Console

from tweetxvault.auth import ResolvedAuthBundle, resolve_auth_bundle
from tweetxvault.client.base import AdaptiveRequestPacer, build_async_client
from tweetxvault.client.timelines import (
    MAX_CONSECUTIVE_FOCAL_ABSENCES,
    FocalResultKind,
    FocalTweetDetailResult,
    TimelineTweet,
    build_tweet_detail_url,
    fetch_page,
    parse_tweet_detail_response,
    parse_tweet_detail_tweets,
)
from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.exceptions import (
    APIResponseError,
    AuthExpiredError,
    ConfigError,
    FeatureFlagDriftError,
    QueryIdRefreshError,
    RateLimitExhaustedError,
    RepeatedFocalAbsenceError,
    StaleQueryIdError,
)
from tweetxvault.extractor import extract_status_id_from_url
from tweetxvault.jobs import locked_archive_job, resolve_job_context
from tweetxvault.pipeline import current_pipeline
from tweetxvault.query_ids import QueryIdStore, refresh_query_ids
from tweetxvault.storage import ArchiveStore
from tweetxvault.utils import resolve_query_ids

_SCAN_PROGRESS_EVERY = 100


@dataclass(slots=True)
class ThreadExpandResult:
    processed: int = 0
    expanded: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(slots=True)
class _FocalAbsenceTracker:
    consecutive: int = 0

    def observe(self, kind: FocalResultKind, tweet_id: str) -> None:
        if kind != FocalResultKind.ABSENT:
            self.consecutive = 0
            return
        self.consecutive += 1
        if self.consecutive >= MAX_CONSECUTIVE_FOCAL_ABSENCES:
            raise RepeatedFocalAbsenceError(
                f"TweetDetail omitted its requested focal tweet for "
                f"{self.consecutive} consecutive responses; last target was {tweet_id}."
            )


def normalize_thread_target(value: str) -> str:
    candidate = value.strip()
    if candidate.isdigit():
        return candidate
    tweet_id = extract_status_id_from_url(candidate)
    if tweet_id:
        return tweet_id
    raise ConfigError(f"Unsupported thread target '{value}'. Use a tweet ID or x.com status URL.")


def _dedupe_targets(values: list[str]) -> tuple[list[str], int]:
    unique = list(dict.fromkeys(values))
    return unique, len(values) - len(unique)


def _log_thread_status(console: Console, tweet_id: str, message: str) -> None:
    pipeline = current_pipeline()
    if pipeline is not None:
        active = pipeline.active_step
        if active is None:
            return
        rendered = f"Tweet {tweet_id}: {message}"
        if any(marker in message for marker in ("failed (", "ambiguous", "unavailable")):
            category = "unavailable" if "unavailable" in message else "fetch-failure"
            pipeline.issue(rendered, dedupe_key=f"threads:{category}")
        else:
            pipeline.status(active.key, rendered, important=True)
        return
    console.print(f"thread {tweet_id}: {message}", highlight=False)


def _log_threads(console: Console, message: str) -> None:
    pipeline = current_pipeline()
    if pipeline is not None:
        active = pipeline.active_step
        if active is not None:
            pipeline.status(active.key, message, important=True)
        return
    console.print(f"threads: {message}", highlight=False)


def _log_scan_progress(
    console: Console,
    *,
    phase: str,
    scanned: int,
    total: int,
    result: ThreadExpandResult,
) -> None:
    if current_pipeline() is not None:
        return
    if scanned % _SCAN_PROGRESS_EVERY != 0 and scanned != total:
        return
    _log_threads(
        console,
        f"{phase} {scanned}/{total} scanned, "
        f"{result.processed} processed, "
        f"{result.expanded} expanded, "
        f"{result.skipped} skipped, "
        f"{result.failed} failed",
    )


async def _fetch_detail(
    *,
    tweet_id: str,
    query_ids: dict[str, str],
    query_store: QueryIdStore,
    client: httpx.AsyncClient,
    config: AppConfig,
    console: Console,
    pacer: AdaptiveRequestPacer | None = None,
) -> tuple[dict[str, object], list[TimelineTweet], FocalTweetDetailResult, int]:
    async def refresh_once() -> str:
        refreshed = await refresh_query_ids(
            query_store,
            operations=["TweetDetail"],
            client=client,
        )
        query_ids.update(refreshed)
        return build_tweet_detail_url(query_ids["TweetDetail"], tweet_id)

    response = await fetch_page(
        client,
        build_tweet_detail_url(query_ids["TweetDetail"], tweet_id),
        config.sync,
        max_retries=config.sync.detail_max_retries,
        backoff_base=config.sync.detail_backoff_base,
        refresh_once=refresh_once,
        status=lambda message: _log_thread_status(console, tweet_id, message),
    )
    if pacer is not None:
        pacer.observe(response)
    payload = response.json()
    tweets = parse_tweet_detail_tweets(payload)
    focal = parse_tweet_detail_response(payload, tweet_id)
    return payload, tweets, focal, response.status_code


async def _expand_target(
    *,
    tweet_id: str,
    store: ArchiveStore,
    query_ids: dict[str, str],
    query_store: QueryIdStore,
    client: httpx.AsyncClient,
    config: AppConfig,
    console: Console,
    pacer: AdaptiveRequestPacer | None = None,
    mark_dirty: Callable[[int, int], None] | None = None,
) -> tuple[list[str], FocalResultKind]:
    payload, tweets, focal, http_status = await _fetch_detail(
        tweet_id=tweet_id,
        query_ids=query_ids,
        query_store=query_store,
        client=client,
        config=config,
        console=console,
        pacer=pacer,
    )
    if focal.kind == FocalResultKind.AVAILABLE:
        store.persist_thread_detail(
            focal_tweet_id=tweet_id,
            tweets=tweets,
            raw_json=payload,
            http_status=http_status,
        )
    elif focal.kind == FocalResultKind.EXPLICIT_UNAVAILABLE:
        assert focal.unavailable is not None
        from tweetxvault.resurrection import resurrection_retry_schedule

        retry_eligible, next_retry_at = resurrection_retry_schedule(
            focal.unavailable.reason,
            0,
        )
        store.persist_unavailable_tweet(
            tweet_id=tweet_id,
            operation="ThreadExpandDetail",
            raw_json=payload,
            http_status=http_status,
            reason=focal.unavailable.reason,
            detail=focal.unavailable.detail,
            retry_eligible=retry_eligible,
            next_retry_at=next_retry_at,
            retry_count=0,
        )
    if mark_dirty is not None and focal.kind != FocalResultKind.ABSENT:
        mark_dirty(1, 1)
    return [tweet.tweet_id for tweet in tweets], focal.kind


async def _try_expand_target(
    *,
    tweet_id: str,
    store: ArchiveStore,
    query_ids: dict[str, str],
    query_store: QueryIdStore,
    client: httpx.AsyncClient,
    config: AppConfig,
    attempted_targets: set[str],
    expanded_targets: set[str],
    known_tweet_ids: set[str],
    result: ThreadExpandResult,
    console: Console,
    absence_tracker: _FocalAbsenceTracker,
    pacer: AdaptiveRequestPacer | None = None,
    mark_dirty: Callable[[int, int], None] | None = None,
) -> None:
    result.processed += 1
    attempted_targets.add(tweet_id)
    try:
        discovered_ids, focal_kind = await _expand_target(
            tweet_id=tweet_id,
            store=store,
            query_ids=query_ids,
            query_store=query_store,
            client=client,
            config=config,
            console=console,
            pacer=pacer,
            mark_dirty=mark_dirty,
        )
    except (
        AuthExpiredError,
        FeatureFlagDriftError,
        QueryIdRefreshError,
        RateLimitExhaustedError,
        RepeatedFocalAbsenceError,
        StaleQueryIdError,
    ):
        raise
    except (APIResponseError, httpx.TransportError) as exc:
        result.failed += 1
        _log_thread_status(console, tweet_id, f"failed ({exc})")
        return
    except Exception:
        raise

    absence_tracker.observe(focal_kind, tweet_id)
    if focal_kind == FocalResultKind.ABSENT:
        result.failed += 1
        _log_thread_status(console, tweet_id, "ambiguous focal absence (deferred)")
        return
    expanded_targets.add(tweet_id)
    if focal_kind == FocalResultKind.EXPLICIT_UNAVAILABLE:
        result.failed += 1
        _log_thread_status(console, tweet_id, "terminal unavailable (classified for retry)")
        return
    known_tweet_ids.update(discovered_ids)
    result.expanded += 1


async def expand_threads(
    *,
    targets: list[str] | None = None,
    limit: int | None = None,
    refresh: bool = False,
    config: AppConfig | None = None,
    paths: XDGPaths | None = None,
    auth_bundle: ResolvedAuthBundle | None = None,
    auth_status: Callable[[str], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    console: Console | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ThreadExpandResult:
    config, paths = resolve_job_context(config=config, paths=paths)
    console = console or Console(stderr=True)
    pipeline = current_pipeline()
    _log_threads(console, "preparing archive expansion job")
    if refresh and not targets:
        raise ConfigError("--refresh requires one or more explicit thread targets.")
    prepare_step_key = "threads-prepare"
    if pipeline is not None:
        pipeline.add_step(
            prepare_step_key,
            "Prepare threads",
            total=2 if auth_bundle is None else 1,
            unit="checks",
            detail="X authentication and TweetDetail operation metadata",
            rate_unit="checks/s",
        )
        pipeline.start_step(
            prepare_step_key,
            activity=(
                "Resolving X authentication"
                if auth_bundle is None
                else "Resolving the TweetDetail operation ID"
            ),
        )
    if auth_bundle is None:
        _log_threads(console, "resolving auth bundle")
        auth_bundle = resolve_auth_bundle(config, status=auth_status)
        if pipeline is not None:
            pipeline.update_step(
                prepare_step_key,
                completed=1,
                activity="Resolving the TweetDetail operation ID",
                counters="X authentication resolved",
            )

    async with locked_archive_job(config=config, paths=paths, console=console) as job:
        store = job.store
        _log_threads(console, "resolving TweetDetail query ID")
        query_store = QueryIdStore(paths)
        query_ids = await resolve_query_ids(
            query_store,
            ["TweetDetail"],
            force_refresh=not query_store.is_fresh(),
            transport=transport,
        )
        if pipeline is not None:
            pipeline.complete_step(
                prepare_step_key,
                "X authentication and TweetDetail operation ID ready",
            )
        result = ThreadExpandResult()
        client = build_async_client(
            auth_bundle,
            timeout=config.sync.timeout,
            transport=transport,
        )
        try:
            _log_threads(console, "loading archived thread expansion state...")
            expanded_targets = set(store.list_raw_capture_target_ids("ThreadExpandDetail"))
            _log_threads(
                console,
                f"loaded {len(expanded_targets)} previously expanded thread targets",
            )
            attempted_targets: set[str] = set()
            known_tweet_ids: set[str] = set()
            pacer = AdaptiveRequestPacer(config.sync.detail_delay)
            absence_tracker = _FocalAbsenceTracker()
            step_key = "threads"
            pipeline_started = False
            pipeline_scanned = 0

            def start_pipeline(total: int, *, activity: str, detail: str) -> None:
                nonlocal pipeline_started
                if pipeline is None or pipeline_started:
                    return
                pipeline.add_step(
                    step_key,
                    "Threads",
                    total=max(total, 1),
                    unit="candidates",
                    detail=detail,
                    rate_unit="candidates/s",
                )
                pipeline.start_step(
                    step_key,
                    activity=activity,
                    counters=(
                        f"0 scanned · {result.processed} fetched · {result.expanded} expanded · "
                        f"{result.skipped} already known · {result.failed} failed"
                    ),
                )
                pipeline_started = True

            def update_pipeline(*, scanned: int, total: int, activity: str | None = None) -> None:
                nonlocal pipeline_scanned
                if pipeline is None or not pipeline_started:
                    return
                pipeline_scanned = scanned
                pipeline.update_step(
                    step_key,
                    completed=scanned,
                    total=max(total, 1),
                    activity=activity,
                    counters=(
                        f"{scanned} scanned · {result.processed} fetched · "
                        f"{result.expanded} expanded · {result.skipped} already known · "
                        f"{result.failed} failed"
                    ),
                )

            if targets:
                requested, duplicate_count = _dedupe_targets(
                    [normalize_thread_target(target) for target in targets]
                )
                result.skipped += duplicate_count
                target_ids = requested[:limit] if limit is not None else requested
                pending_target_ids = [
                    tweet_id
                    for tweet_id in target_ids
                    if refresh or tweet_id not in expanded_targets
                ]
                if pending_target_ids:
                    start_pipeline(
                        len(target_ids),
                        activity=f"Preparing explicit thread target {pending_target_ids[0]}",
                        detail=(
                            f"{len(target_ids)} explicit targets"
                            + (" · refresh requested" if refresh else " · prior expansions reused")
                        ),
                    )
                refresh_suffix = " (refresh)" if refresh else ""
                _log_threads(
                    console,
                    f"explicit target pass over {len(target_ids)} targets{refresh_suffix}",
                )
                for scanned, tweet_id in enumerate(target_ids, start=1):
                    if not refresh and tweet_id in expanded_targets:
                        result.skipped += 1
                        update_pipeline(scanned=scanned, total=len(target_ids))
                        _log_scan_progress(
                            console,
                            phase="explicit",
                            scanned=scanned,
                            total=len(target_ids),
                            result=result,
                        )
                        continue
                    if pipeline_started and pipeline is not None:
                        pipeline.update_step(
                            step_key,
                            completed=scanned - 1,
                            activity=f"Fetching thread context for tweet {tweet_id}",
                        )
                    await pacer.wait(attempted=result.processed, sleep=sleep)
                    await _try_expand_target(
                        tweet_id=tweet_id,
                        store=store,
                        query_ids=query_ids,
                        query_store=query_store,
                        client=client,
                        config=config,
                        attempted_targets=attempted_targets,
                        expanded_targets=expanded_targets,
                        known_tweet_ids=known_tweet_ids,
                        result=result,
                        console=console,
                        absence_tracker=absence_tracker,
                        pacer=pacer,
                        mark_dirty=job.mark_dirty,
                    )
                    _log_scan_progress(
                        console,
                        phase="explicit",
                        scanned=scanned,
                        total=len(target_ids),
                        result=result,
                    )
                    update_pipeline(scanned=scanned, total=len(target_ids))
                    job.maybe_optimize_mid_job(console=console)
            else:
                _log_threads(console, "loading archived membership tweets...")
                membership_ids = store.list_membership_tweet_ids()
                pending_membership_ids = [
                    tweet_id for tweet_id in membership_ids if tweet_id not in expanded_targets
                ]
                if pending_membership_ids:
                    start_pipeline(
                        len(membership_ids),
                        activity=(
                            f"Preparing membership thread target {pending_membership_ids[0]}"
                        ),
                        detail=(
                            f"membership pass · {len(membership_ids)} archived tweets · "
                            f"{len(expanded_targets)} prior expansions"
                        ),
                    )
                _log_threads(
                    console,
                    "membership pass over "
                    f"{len(membership_ids)} archived tweets "
                    f"({len(expanded_targets)} already expanded)",
                )
                for scanned, tweet_id in enumerate(membership_ids, start=1):
                    if limit is not None and result.processed >= limit:
                        break
                    if tweet_id in expanded_targets:
                        result.skipped += 1
                        update_pipeline(scanned=scanned, total=len(membership_ids))
                        _log_scan_progress(
                            console,
                            phase="membership",
                            scanned=scanned,
                            total=len(membership_ids),
                            result=result,
                        )
                        continue
                    if pipeline_started and pipeline is not None:
                        pipeline.update_step(
                            step_key,
                            completed=scanned - 1,
                            activity=f"Fetching thread context for tweet {tweet_id}",
                        )
                    await pacer.wait(attempted=result.processed, sleep=sleep)
                    await _try_expand_target(
                        tweet_id=tweet_id,
                        store=store,
                        query_ids=query_ids,
                        query_store=query_store,
                        client=client,
                        config=config,
                        attempted_targets=attempted_targets,
                        expanded_targets=expanded_targets,
                        known_tweet_ids=known_tweet_ids,
                        result=result,
                        console=console,
                        absence_tracker=absence_tracker,
                        pacer=pacer,
                        mark_dirty=job.mark_dirty,
                    )
                    _log_scan_progress(
                        console,
                        phase="membership",
                        scanned=scanned,
                        total=len(membership_ids),
                        result=result,
                    )
                    update_pipeline(scanned=scanned, total=len(membership_ids))
                    job.maybe_optimize_mid_job(console=console)

                if limit is None or result.processed < limit:
                    _log_threads(console, "loading known tweet ids for linked-status pass...")
                    known_tweet_ids = store.list_known_tweet_ids()
                    _log_threads(
                        console,
                        f"loaded {len(known_tweet_ids)} known tweet ids for linked-status dedupe",
                    )
                    _log_threads(console, "loading archived url refs...")
                    url_ref_rows = store.list_url_ref_rows()

                    max_linked_depth = config.sync.max_linked_depth

                    edges = {}
                    for row in url_ref_rows:
                        target_id = None
                        for field_name in ("canonical_url", "expanded_url", "url"):
                            candidate = row.get(field_name)
                            if isinstance(candidate, str):
                                target_id = extract_status_id_from_url(candidate)
                                if target_id:
                                    break
                        src = row.get("tweet_id")
                        if src and target_id:
                            if src not in edges:
                                edges[src] = []
                            edges[src].append((target_id, row))

                    depths = {tid: 0 for tid in membership_ids if tid}
                    for d in range(1, max_linked_depth + 1):
                        current_layer = [tid for tid, depth in depths.items() if depth == d - 1]
                        for src in current_layer:
                            if src in edges:
                                for tgt_id, _ in edges[src]:
                                    if tgt_id not in depths:
                                        depths[tgt_id] = d

                    filtered_url_rows = []
                    for src, target_list in edges.items():
                        if max_linked_depth == 0:
                            break
                        if src not in depths or depths[src] >= max_linked_depth:
                            continue
                        for _, row in target_list:
                            filtered_url_rows.append(row)

                    _log_threads(
                        console,
                        "linked-status pass over "
                        f"{len(filtered_url_rows)} reachable url refs "
                        f"(from {len(url_ref_rows)} total, "
                        f"max depth {max_linked_depth})",
                    )
                    linked_offset = pipeline_scanned if pipeline_started else 0
                    if filtered_url_rows and not pipeline_started:
                        start_pipeline(
                            len(filtered_url_rows),
                            activity="Preparing reachable linked-status targets",
                            detail=(
                                f"linked-status pass · max depth {max_linked_depth} · "
                                f"{len(url_ref_rows)} saved URL refs"
                            ),
                        )
                        linked_offset = 0
                    elif filtered_url_rows and pipeline is not None and pipeline_started:
                        pipeline.update_step(
                            step_key,
                            completed=pipeline_scanned,
                            total=pipeline_scanned + len(filtered_url_rows),
                            activity="Preparing reachable linked-status targets",
                            detail=(
                                f"membership + linked-status · max depth {max_linked_depth} · "
                                f"{len(filtered_url_rows)} reachable URL refs"
                            ),
                        )
                    for scanned, row in enumerate(filtered_url_rows, start=1):
                        if limit is not None and result.processed >= limit:
                            break
                        target_id = None
                        for field_name in ("canonical_url", "expanded_url", "url"):
                            candidate = row.get(field_name)
                            if isinstance(candidate, str):
                                target_id = extract_status_id_from_url(candidate)
                                if target_id:
                                    break
                        if not target_id:
                            update_pipeline(
                                scanned=linked_offset + scanned,
                                total=linked_offset + len(filtered_url_rows),
                            )
                            _log_scan_progress(
                                console,
                                phase="linked-status",
                                scanned=scanned,
                                total=len(filtered_url_rows),
                                result=result,
                            )
                            continue
                        source_tweet_id = row.get("tweet_id")
                        if (
                            target_id == source_tweet_id
                            or target_id in attempted_targets
                            or target_id in expanded_targets
                            or target_id in known_tweet_ids
                        ):
                            result.skipped += 1
                            update_pipeline(
                                scanned=linked_offset + scanned,
                                total=linked_offset + len(filtered_url_rows),
                            )
                            _log_scan_progress(
                                console,
                                phase="linked-status",
                                scanned=scanned,
                                total=len(filtered_url_rows),
                                result=result,
                            )
                            continue
                        if pipeline_started and pipeline is not None:
                            pipeline.update_step(
                                step_key,
                                completed=linked_offset + scanned - 1,
                                activity=f"Fetching linked tweet context for {target_id}",
                            )
                        await pacer.wait(attempted=result.processed, sleep=sleep)
                        await _try_expand_target(
                            tweet_id=target_id,
                            store=store,
                            query_ids=query_ids,
                            query_store=query_store,
                            client=client,
                            config=config,
                            attempted_targets=attempted_targets,
                            expanded_targets=expanded_targets,
                            known_tweet_ids=known_tweet_ids,
                            result=result,
                            console=console,
                            absence_tracker=absence_tracker,
                            pacer=pacer,
                            mark_dirty=job.mark_dirty,
                        )
                        _log_scan_progress(
                            console,
                            phase="linked-status",
                            scanned=scanned,
                            total=len(filtered_url_rows),
                            result=result,
                        )
                        update_pipeline(
                            scanned=linked_offset + scanned,
                            total=linked_offset + len(filtered_url_rows),
                        )
                        job.maybe_optimize_mid_job(console=console)
        finally:
            await client.aclose()

        if pipeline is not None and pipeline_started:
            if pipeline_scanned <= 0:
                pipeline_scanned = result.processed + result.skipped
            pipeline.update_step(
                step_key,
                completed=max(pipeline_scanned, 1),
                total=max(pipeline_scanned, 1),
            )
            pipeline.complete_step(
                step_key,
                f"{result.processed} fetched · {result.expanded} expanded · "
                f"{result.skipped} already known · {result.failed} failed",
            )
        return result
