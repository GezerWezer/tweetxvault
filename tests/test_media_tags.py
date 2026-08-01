from __future__ import annotations

import json

from tweetxvault.storage import open_archive_store


def _tag_payload(store, tweet_id: str) -> dict[str, object] | None:
    row = store.conn.execute(
        "SELECT raw_json FROM archive WHERE record_type = 'media_tag' AND tweet_id = ?",
        (tweet_id,),
    ).fetchone()
    return json.loads(row["raw_json"]) if row is not None else None


def _seed_tag_candidate(
    store,
    tweet_id: str,
    *,
    created_at_ts: int,
    enrichment_state: str = "done",
    with_media: bool = True,
) -> None:
    rows = [
        store._record(
            row_key=f"tweet:bookmark::{tweet_id}",
            record_type="tweet",
            tweet_id=tweet_id,
            collection_type="bookmark",
            created_at_ts=created_at_ts,
            text=f"tweet {tweet_id}",
            raw_json=f'{{"id":"{tweet_id}"}}',
        ),
        store._record(
            row_key=f"tweet_object:{tweet_id}",
            record_type="tweet_object",
            tweet_id=tweet_id,
            enrichment_state=enrichment_state,
        ),
    ]
    if with_media:
        rows.extend(
            [
                store._record(
                    row_key=f"media:{tweet_id}:first",
                    record_type="media",
                    tweet_id=tweet_id,
                    media_key="first",
                ),
                store._record(
                    row_key=f"media:{tweet_id}:second",
                    record_type="media",
                    tweet_id=tweet_id,
                    media_key="second",
                ),
            ]
        )
    store._merge_records(rows)


def test_tagging_eligibility_filters_dedupes_orders_and_limits(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_tag_candidate(store, "newest", created_at_ts=30)
    _seed_tag_candidate(store, "older", created_at_ts=20)
    _seed_tag_candidate(store, "resurrected", created_at_ts=25, enrichment_state="resurrected")
    _seed_tag_candidate(store, "pending", created_at_ts=50, enrichment_state="pending")
    _seed_tag_candidate(store, "no-media", created_at_ts=40, with_media=False)
    _seed_tag_candidate(store, "tagged", created_at_ts=60)
    store.update_media_tags("tagged", ["Existing"])

    assert store.get_eligible_tweets_for_tagging(limit=1) == ["newest"]
    assert store.get_eligible_tweets_for_tagging(limit=20) == [
        "newest",
        "resurrected",
        "older",
    ]
    store.close()


def test_update_media_tags_creates_normalizes_and_is_idempotent(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None

    store.update_media_tags("1", [" Nature ", "nature", "", "SKY", "sky"])
    first = _tag_payload(store, "1")
    store.update_media_tags("1", ["Nature", "SKY"])
    second = _tag_payload(store, "1")

    assert first == second == {"description": "", "tags": ["Nature", "SKY"]}
    assert (
        store.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE record_type = 'media_tag' AND tweet_id = '1'"
        ).fetchone()[0]
        == 1
    )
    store.close()


def test_update_media_tags_preserves_description_and_recovers_invalid_json(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store._merge_records(
        [
            store._record(
                row_key="media_tag:1",
                record_type="media_tag",
                tweet_id="1",
                raw_json=json.dumps({"description": "A scene", "tags": ["Old"]}),
            ),
            store._record(
                row_key="media_tag:2",
                record_type="media_tag",
                tweet_id="2",
                raw_json="{broken",
            ),
        ]
    )

    store.update_media_tags("1", ["New"])
    store.update_media_tags("2", ["Recovered"])

    assert _tag_payload(store, "1") == {"description": "A scene", "tags": ["New"]}
    assert _tag_payload(store, "2") == {"tags": ["Recovered"]}
    store.close()


def test_empty_update_and_delete_media_tag_remove_row(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store.update_media_tags("1", ["Nature"])
    store.update_media_tags("1", [" ", ""])
    assert _tag_payload(store, "1") is None

    store.update_media_tags("1", ["Nature"])
    store.delete_media_tag("1")
    store.delete_media_tag("missing")
    assert _tag_payload(store, "1") is None
    store.close()


def test_delete_global_tag_is_case_insensitive_and_removes_empty_rows(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store.update_media_tags("1", ["Nature", "Sky"])
    store.update_media_tags("2", ["NATURE"])
    store._merge_records(
        [
            store._record(
                row_key="media_tag:broken",
                record_type="media_tag",
                tweet_id="broken",
                raw_json="{not-json",
            )
        ]
    )

    store.delete_global_tag("nature")

    assert _tag_payload(store, "1") == {"description": "", "tags": ["Sky"]}
    assert _tag_payload(store, "2") is None
    assert (
        store.conn.execute("SELECT raw_json FROM archive WHERE tweet_id = 'broken'").fetchone()[0]
        == "{not-json"
    )
    store.close()


def test_merge_global_tags_is_case_insensitive_and_deduplicates(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store.update_media_tags("1", ["Landscape", "Nature", "SKY", "sky"])
    store.update_media_tags("2", ["nature", "Portrait"])
    store.update_media_tags("3", ["Unrelated"])

    store.merge_global_tags("Scenery", [" nature ", "SKY", "scenery", "NATURE"])

    assert _tag_payload(store, "1")["tags"] == ["Landscape", "Scenery"]
    assert _tag_payload(store, "2")["tags"] == ["Portrait", "Scenery"]
    assert _tag_payload(store, "3")["tags"] == ["Unrelated"]
    store.close()


def test_merge_global_tags_noops_for_blank_primary_or_empty_sources(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store.update_media_tags("1", ["Nature"])

    store.merge_global_tags("", ["Nature"])
    store.merge_global_tags("Scenery", [])
    store.merge_global_tags("Nature", ["nature", " NATURE "])

    assert _tag_payload(store, "1")["tags"] == ["Nature"]
    store.close()


def test_tag_counts_support_query_unlimited_results_and_malformed_rows(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store.update_media_tags("1", ["Nature", "Sky"])
    store.update_media_tags("2", ["nature", "City"])
    store.update_media_tags("3", ["Portrait"])
    store._merge_records(
        [
            store._record(
                row_key="media_tag:broken",
                record_type="media_tag",
                tweet_id="broken",
                raw_json="{broken",
            )
        ]
    )

    all_counts = store.get_tag_counts(limit=-1)
    nature_counts = store.get_tag_counts(query="NAT", limit=50)
    limited = store.get_tag_counts(limit=1)

    assert len(all_counts) == 4
    assert nature_counts == [{"tag": "Nature", "count": 2}]
    assert len(limited) == 1
    assert limited[0]["count"] == 2
    store.close()


def test_export_includes_valid_tags_and_skips_malformed_tag_json(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store._merge_records(
        [
            store._record(
                row_key="tweet:bookmark::1",
                record_type="tweet",
                tweet_id="1",
                collection_type="bookmark",
                text="one",
                raw_json='{"id":"1"}',
            ),
            store._record(
                row_key="tweet:bookmark::2",
                record_type="tweet",
                tweet_id="2",
                collection_type="bookmark",
                text="two",
                raw_json='{"id":"2"}',
            ),
        ]
    )
    store.update_media_tags("1", ["Nature"])
    store._merge_records(
        [
            store._record(
                row_key="media_tag:2",
                record_type="media_tag",
                tweet_id="2",
                raw_json="{broken",
            )
        ]
    )

    exported = {row["tweet_id"]: row for row in store.export_rows("bookmark")}

    assert exported["1"]["media_tags"]["tags"] == ["Nature"]
    assert exported["2"]["media_tags"] is None
    store.close()
