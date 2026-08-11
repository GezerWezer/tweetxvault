# tweetxvault

> **Fork Note:** This repository has substantially diverged from the original upstream repository. The original upstream uses LanceDB, PyArrow, and ONNX vector embeddings for its core storage and search. **This fork has completely scrapped LanceDB and vector embeddings** in favor of native SQLite and FTS5 due to extreme instability, OOM crashes, and Rust panics during vector indexing. It also introduces a fully interactive FastAPI/Vue.js Web UI, daemonization commands, advanced search operators, degree-of-separation thread limits, and dead-tweet tracking. If you are updating from the original upstream, see the [Migration](#migrating-from-lancedb) section below.

A Python CLI tool for archiving your Twitter/X bookmarks, likes, and authored tweets into a local SQLite database, with support for importing official X archive exports into the same store. Runs unattended via cron, supports incremental sync with crash-safe resume, and preserves raw API responses so you never lose data.

<img src="https://raw.githubusercontent.com/lhl/tweetxvault/main/docs/screenshot.png" alt="tweetxvault view all" width="800">

## Features

### New Fork Additions
- **Interactive Web UI** — browse your archive through a local web server with thread navigation, article cards, and theme support
- **Unavailable-post continuity** — protected, suspended, deleted, and otherwise missing posts remain visible as reason-aware placeholders instead of disappearing from feeds or threads
- **Advanced full-text search** — built-in native SQLite FTS5 search supporting exact phrases, exclusions, and Twitter-style operators (`from:`, `min_faves:`, `filter:images`)
- **Tombstone tracking** — safely records deleted/suspended tweets to prevent infinite request loops, and periodically attempts to "resurrect" them
- **Thread depth limits** — prevents infinite web-crawler-like snowballing by strictly enforcing degrees of separation from your root bookmarks and likes

### Original Upstream Features
- **Incremental sync** — fetches only new items by default; resumes interrupted backfills automatically
- **Official X archive import** — imports authored tweets, deleted tweets, likes, and exported media from official X archive ZIPs/directories into the same local archive
- **Raw capture preservation** — every API response page is stored verbatim alongside parsed tweet records
- **Secondary object extraction** — archives canonical tweet objects, attached-tweet relations, media metadata, URL refs, and article payloads alongside collection memberships
- **Crash-safe checkpoints** — sync state advances atomically with data writes; safe to kill mid-run
- **Automatic query ID discovery** — scrapes Twitter's JS bundles to stay current with GraphQL endpoint changes
- **Browser cookie extraction** — reads session cookies from Firefox plus Chromium-family browsers like Chrome, Chromium, Brave, Edge, Opera, Opera GX, Vivaldi, and Arc
- **Rate limit handling** — exponential backoff, cooldown periods, and configurable retry limits
- **Export** — export your archive to JSON or a self-contained HTML viewer

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Unix-like runtime only today (Linux/macOS). Windows is not supported yet because the CLI currently depends on `fcntl`, `resource`, and `strftime("%-d")`.
- A Twitter/X account logged in via Firefox or a supported Chromium-family browser, or session cookies obtained manually

## Installation

Install from PyPI:

```bash
pip install tweetxvault
```

Install globally with `uv`:

```bash
uv tool install tweetxvault
```

Install globally with `pipx`:

```bash
pipx install tweetxvault
```

To enable the interactive web UI:

```bash
pip install "tweetxvault[web]"
```

Or install the extra as a global tool:

```bash
uv tool install "tweetxvault[web]"
pipx install "tweetxvault[web]"
```

Install from source:

```bash
git clone https://github.com/gezerwezer/tweetxvault.git
cd tweetxvault
uv sync
```

Install your local checkout as a global editable tool while developing on `HEAD`:

```bash
uv tool install -e .
```

Re-run that command with `--force` after dependency or metadata changes in
`pyproject.toml`.

Use `tweetxvault --version` to confirm which local build you are running. In a
git checkout, the CLI includes the short commit hash and appends `dirty` when
tracked files differ from `HEAD`.

Run once without installing:

```bash
uvx tweetxvault --help
```

### Migrating from LanceDB

If you used the original upstream repository previously, your data is stored in the legacy LanceDB format (`archive.lancedb`). This fork uses native SQLite (`archive.db`) and completely drops the `[embed]` extra and vector embedding models for stability reasons. 

To safely port your data over to the new format:

```bash
uv run tweetxvault migrate
```

The migration tool reads from the old `archive.lancedb` table with LanceDB 0.34 and writes into the new native SQLite database. It loads data into memory in controlled chunks to prevent OOM errors and isolates the LanceDB driver in a subprocess to protect against Rust core panics (`index out of bounds`) during index compaction. If a source chunk cannot be read, the migrator retries progressively smaller ranges down to one row so that a damaged row does not discard the rest of its chunk. A partial migration reports the skipped row count and tells you to keep the old LanceDB directory; after a complete migration, you can manually delete it after verifying the result.

## Authentication

tweetxvault needs your `auth_token` and `ct0` session cookies from Twitter/X. There are three ways to provide them (checked in this order):

### 1. Environment variables (simplest)

```bash
export TWEETXVAULT_AUTH_TOKEN="your_auth_token"
export TWEETXVAULT_CT0="your_ct0_token"
export TWEETXVAULT_USER_ID="your_numeric_user_id"  # required for likes and own-tweet sync
```

### 2. Config file

tweetxvault creates `~/.config/tweetxvault/config.toml` during normal initialization with this
permanent authentication skeleton:

```toml
[auth]
auth_token = ""
ct0 = ""
user_id = ""
```

Replace the empty strings to store credentials in the file. Empty values remain unset and do not
interfere with environment variables or browser-cookie extraction.

### 3. Browser auto-extraction

If you're logged into x.com in Firefox, Chrome, Chromium, Brave, Edge, Opera, Opera GX, Vivaldi, or Arc, tweetxvault will try them in that order and stop after the first browser profile that yields valid X cookies.

Firefox is read from its profile database directly. Chromium-family browsers use `browser-cookie3` for cookie decryption and OS keyring access.

To force a specific browser or profile for one command:

```bash
uv run tweetxvault auth check --browser chrome
uv run tweetxvault sync --browser brave --profile "Profile 2"
uv run tweetxvault sync --browser firefox --profile-path /path/to/profile
```

How `--browser` behaves:

- `--browser`, `--profile`, and `--profile-path` force tweetxvault to take `auth_token` and `ct0` from that browser/profile.
- `user_id` still uses the normal precedence order: `TWEETXVAULT_USER_ID` -> `auth.user_id` -> browser `twid`.
- That means you can still pin `user_id` explicitly for likes or authored-tweet sync if needed, even while forcing cookies from a specific browser profile.
- If you do not set `user_id` explicitly, tweetxvault will use the browser profile's `twid` cookie when available.

For example, this uses Firefox cookies from the selected profile, but still pins `user_id` from the environment:

```bash
export TWEETXVAULT_USER_ID="123456789"
uv run tweetxvault sync likes --browser firefox --profile my-profile
```

This matters most if you use multiple X accounts. Make sure the selected browser profile and resolved `user_id` belong to the same account. If you mix cookies from one account with a `user_id` from another, likes/authored-tweet sync may fail, and tweetxvault's archive-owner guardrail will refuse writes if the local archive already belongs to a different user.

Run `uv run tweetxvault auth check --browser ...` first if you want to verify which sources are being used before a sync.

To persist a browser preference in the environment or config:

```bash
export TWEETXVAULT_BROWSER="chrome"
export TWEETXVAULT_BROWSER_PROFILE="Profile 2"
export TWEETXVAULT_BROWSER_PROFILE_PATH="/path/to/profile"
```

Legacy Firefox-only override is still supported:

```bash
export TWEETXVAULT_FIREFOX_PROFILE_PATH="/path/to/your/firefox/profile"
```

### Verify your setup

```bash
uv run tweetxvault auth check
uv run tweetxvault auth check --interactive
```

This probes the API without writing any data and reports credential status and endpoint readiness. `--interactive` opens a picker over discovered browser profiles with valid X cookies.

## Usage

### Syncing

```bash
# Normal archive maintenance: sync bookmarks + likes, then run thread expansion,
# bounded resurrection checks, article refresh, media download, and unfurl.
uv run tweetxvault sync

# Explicit alias for the same default sync pass
uv run tweetxvault sync all

# Sync just bookmarks, likes, or your own authored tweets
uv run tweetxvault sync bookmarks
uv run tweetxvault sync likes
uv run tweetxvault sync tweets

# Force a specific browser profile for this run
uv run tweetxvault sync --browser chrome --profile "Profile 2"

# Full re-sync from scratch (resets sync state, does not delete existing data)
uv run tweetxvault sync --full

# Continue past duplicates without resetting state
uv run tweetxvault sync --backfill

# Clear a saved historical backfill cursor and run only the head pass
uv run tweetxvault sync likes --head-only

# Rewalk existing pages to refresh article-bearing tweets after article fields change
uv run tweetxvault sync bookmarks --article-backfill

# Limit to N pages per collection
uv run tweetxvault sync --limit 5

# Opt out of one or more automatic follow-up jobs for this run
uv run tweetxvault sync --skip-media --skip-unfurl
```

`--article-backfill` updates stored `raw_json` and normalized secondary rows inline, so it does not require a follow-up `tweetxvault rehydrate`.
By default, `tweetxvault sync` and `tweetxvault sync all` both cover bookmarks + likes, then visibly run thread expansion, bounded unavailable-tweet resurrection checks, preview-only article refresh, media download, unfurl, and configured AI tagging. Initial X-archive enrichment is a separate finite import job and never runs during ordinary sync. Authored tweets stay opt-in via `tweetxvault sync tweets`.
`--head-only` is the escape hatch when an old saved backfill cursor is no longer useful: it clears that cursor for the targeted collection and runs only the normal head pass. It cannot be combined with `--full`, `--backfill`, or `--article-backfill`.

Interactive long-running commands use one pipeline display for the full command lifecycle. A
sync declares its full flag-relevant lifecycle before work starts, while steps explicitly removed
with `--skip-*` flags are omitted. A planned step whose queue is empty remains visible with a line
symbol and a specific `skipped due to …` reason. The header shows total command elapsed time and
each completed step keeps its own elapsed time. Exact local queues show determinate totals,
throughput, and a per-step ETA. Tagging avoids a full archive pre-count: bounded runs show the
batch/RPD ceiling, unlimited runs discover work one batch at a time, and both show processed/tagged
counts without a rate or ETA. X timeline pagination does not expose a trustworthy remote total, so
sync shows determinate progress for the current page and durable page/tweet counts without
inventing an overall percentage or ETA.
The side panel is reserved for warnings and recoverable errors; retry activity and current
tweet/file/host details remain attached to their step. The legacy `tweetxvault migrate` command
uses the same lifecycle display for source inspection, row copying, and FTS rebuilding.

Common sync flags:

- `--full`: clear the saved sync state for that collection and start a fresh incremental crawl without deleting stored tweets.
- `--backfill`: keep walking older pages past duplicate detection when you want more history without resetting state.
- `--head-only`: clear a saved older-history cursor and do only the normal head pass; use this to stop `resume older`.
- `--article-backfill`: rewalk existing pages to refresh article-bearing tweets after article extraction changes.
- `--skip-resurrection`, `--skip-threads`, `--skip-articles`, `--skip-media`, `--skip-unfurl`: skip one or more automatic follow-up archive-maintenance jobs for just that sync run.
- `--limit N`: cap the run to `N` fetched pages for debugging, sampling, or shorter catch-up runs.
- `--browser`, `--profile`, `--profile-path`: force a specific browser/profile for cookie extraction on just that run.

Backfill status markers shown by `tweetxvault stats`:

- `resume older`: the next sync will do its normal head pass, then resume older history from a saved cursor.
- `none saved`: no older-history cursor is saved for that collection.
- `saved only`: a cursor exists without the normal incomplete marker; this is an unusual transitional state.
- `incomplete`: the sync state says older history is unfinished but no cursor is currently saved; this is also unusual.

To clear `resume older`, run `tweetxvault sync <collection> --head-only`, for example `tweetxvault sync likes --head-only`.
Use `tweetxvault sync <command> --help` for the current CLI flag descriptions.

### Backfilling an existing archive

If your archive was built before the default follow-up sync behavior landed, or if
you already have imported archive data and want to fill in the missing follow-up
rows later, run the follow-up commands directly:

```bash
# Sparse archive-imported tweet placeholders -> TweetDetail enrichment
uv run tweetxvault import enrich

# Parent/reply/context tweet capture
uv run tweetxvault threads expand

# Preview-only article rows -> full article bodies
uv run tweetxvault articles refresh

# Pending media files
uv run tweetxvault media download

# Saved URLs -> canonical/final URL metadata
uv run tweetxvault unfurl
```

The bare `import enrich` command takes one stable snapshot of every initial-enrichment row
eligible when the command starts, then processes each selected tweet once. Use `--limit N` for
a bounded continuation run. Its progress total is the complete selected queue, while completed
writes are still committed every 100 attempts. A large queue can take hours because TweetDetail
rate-limit pacing remains active. Normal sync never runs this initial-enrichment queue; it only
runs the separate, bounded resurrection pass. The other commands in that follow-up path also
support `--limit`. `media download` and `unfurl` additionally support `--retry-failed` if you
want to revisit rows that previously failed.

### Importing an X archive

```bash
# Import an official X archive and automatically enrich every currently eligible sparse row
uv run tweetxvault import x-archive ~/Downloads/twitter-archive.zip

# Clear previously imported archive-owned rows/media and reimport from scratch
uv run tweetxvault import x-archive ~/Downloads/twitter-archive.zip --regen

# Import and run bulk live reconciliation, but leave sparse TweetDetail work for later
uv run tweetxvault import x-archive ~/Downloads/twitter-archive.zip --no-enrich

# Run a bounded TweetDetail follow-up after the automatic bulk tweets/likes reconciliation
uv run tweetxvault import x-archive ~/Downloads/twitter-archive --detail-lookups 100

# Sample a large archive without touching the normal follow-up path
uv run tweetxvault import x-archive ~/Downloads/twitter-archive.zip --regen --sample-limit 1000

# Continue pending TweetDetail follow-up later without re-reading the archive ZIP
uv run tweetxvault import enrich

# Or restrict the selected snapshot explicitly
uv run tweetxvault import enrich --limit 200
```

The importer maps authored tweets, deleted authored tweets, likes, and exported `tweets_media/` files into the same SQLite archive used by live sync. It applies the same archive-owner guardrail as sync, runs bulk live `tweets` / `likes` reconciliation when auth is available, and then processes the stable snapshot of sparse TweetDetail rows eligible at the start of that phase. Progress is committed in batches. If enrichment is interrupted, the local archive import remains complete and the command prints `tweetxvault import enrich` as the continuation command.

Import follow-up options:
- Default import runs the TweetDetail pass for **all currently eligible** sparse tweets after bulk live reconciliation.
- `--no-enrich` skips only the long per-tweet pass; bulk live reconciliation still runs.
- `--detail-lookups N` bounds automatic TweetDetail enrichment to at most `N` selected sparse tweets for that import invocation.
- `--regen` clears archive-import-owned rows, import manifests, and copied archive media files before reimporting. It leaves live-synced rows intact.

### Importing old "Grailbird" archives (pre-2018)

Twitter archives exported before ~2018 use an older format called "Grailbird" (CSV-based, with `tweets.csv` in the root and monthly JS files under `data/js/tweets/`). These cannot be imported directly — convert them first with the shipped `tweetxvault import grailbird` command:

```bash
# Convert the old archive to modern format
tweetxvault import grailbird ~/TwitterArchive-2015 ~/TwitterArchive-2015-converted

# Then import normally
tweetxvault import x-archive ~/TwitterArchive-2015-converted
```

### Viewing your archive

```bash
# View recent bookmarks in a terminal table
uv run tweetxvault view bookmarks

# View likes, oldest first
uv run tweetxvault view likes --sort oldest

# View your authored tweets
uv run tweetxvault view tweets

# View all archived tweets
uv run tweetxvault view all --limit 50
```

Terminal views render tweet timestamps in your local timezone. Sort order uses tweet `created_at`, not collection position.

### Web UI

If you installed the `web` extra, you can browse your archive through an interactive web interface. The web UI is designed as a fast, local clone of the Twitter interface, allowing you to seamlessly navigate threads, read articles, and browse your bookmarks and likes in a familiar layout with full-text search and light/dark theme support.

**Key Web UI Features:**
- **History API Integration:** Browser back/forward buttons work flawlessly when diving in and out of threads, completely preserving scroll positions with zero flashing or resets.
- **Unavailable Post Placeholders:** Protected, suspended, deleted, withheld, and otherwise unavailable posts keep their feed position and thread topology. Quoted, retweeted, and relation-only posts use the same reason-aware placeholder contract, while transiently incomplete posts retain any captured content. Sparse rows explicitly mark text, quote targets, and media metadata that were not captured.
- **Native Render Fidelity:** Accurate styling for quoted tweets, circular avatars, and native rendering of cyan Twitter Polls. Article cards strip redundant `t.co` links, use `summary_large_image` thumbnails, and are fully clickable.
- **Manual Tag Editing:** Every top-level post has a three-dot menu with tag viewing and editing, including descriptions and untagged posts. Hold the button for one second to open the tag view directly.
- **Archive Analytics:** The overview reports unavailable imported tweets and their archive percentage. Archive status separates enrichment, thread expansion, missing enrichment, and resurrection, with an availability-reason breakdown. Storage shows an inline size breakdown in simplified or detailed form.
- **Daemon Management:** Run the server safely in the background using native CLI daemon commands.

The web server runs as a background daemon so you don't need to keep a terminal open:

```bash
# Start the background web server
uv run tweetxvault web start

# Check if the server is running and see its URL
uv run tweetxvault web status

# Restart the background server
uv run tweetxvault web restart

# Stop the background server
uv run tweetxvault web stop

# Set a secure password for the web UI
uv run tweetxvault web set-password
```

By default, the server runs on `http://127.0.0.1:8000` with the default password `password`. On first boot, the UI will warn you prominently to change this using `tweetxvault web set-password`.

When browsing the web UI, tweetxvault will automatically fetch user avatars directly from Twitter as needed, saving them to `media/avatars`. If you prefer to browse fully offline or want to save space, you can disable this behavior. 

To configure custom ports or avatar fetching, add a `[web]` section to your `config.toml`:

```toml
[web]
host = "127.0.0.1"
port = 8000
fetch_avatars = true
```

### Searching

```bash
# Search posts with the same query language used by the Web UI
uv run tweetxvault search "machine learning"

# Search article titles and bodies explicitly, or narrow post collections
uv run tweetxvault search "machine learning" --type article
uv run tweetxvault search "machine learning" --type post --collection bookmark,like

# Combine Twitter-style filters; ordinary clauses are ANDed by default
uv run tweetxvault search 'from:alice "machine learning" has:article'
uv run tweetxvault search 'from:alice OR from:bob has:image'

# Sort search results chronologically instead of by relevance
uv run tweetxvault search "machine learning" --sort newest
uv run tweetxvault search "machine learning" --sort oldest

# Adjust result count
uv run tweetxvault search "transformer architecture" --limit 50
```

The CLI and Web UI use the same SQLite FTS5-backed post search engine and Twitter-style filters.
Spaces are an implicit `AND`; standalone uppercase `AND` is accepted when useful, and uppercase
`OR` groups the clauses immediately around it while surrounding clauses remain required. Lowercase
`and` and `or` remain ordinary search words. The Web UI continues to provide its existing dropdown
for filter autocomplete.

**Supported Search Operators:**
- `"exact phrase"` — wrap words in quotes to find an exact match
- `AND` — optional explicit form of the default all-clauses-required behavior
- `OR` — `cats OR dogs` matches posts containing either adjacent clause
- `*` — prefix wildcard (e.g., `py*` matches `python`)
- `-` — exclusion (e.g., `cats -dogs`)
- `#hashtag` and `$cashtag` support
- `from:username` / `to:username` — filter by author or recipient
- `since:YYYY-MM-DD` / `until:YYYY-MM-DD` — filter by date range
- `min_faves:N` / `min_retweets:N` / `min_replies:N` — filter by engagement thresholds
- `has:image` / `has:video` / `has:media` / `has:links` — filter by attached content
- `has:article` — match posts with an attached archived article
- `is:reply` / `is:quote` / `is:retweet` / `is:thread` — filter by post type
- `is:verified` — match posts from verified users
- `is:resurrected` — match previously unavailable posts that became accessible again

Legacy `filter:` spellings remain accepted for compatibility but are no longer suggested by the
Web autocomplete.

Filters:
- `--type` — comma-delimited result types: `post` (default), `article`
- `--collection` — comma-delimited archive collections: `bookmark`, `like`, `tweet`
- `--sort` — `relevance` (default), `newest`, or `oldest`

### Exporting

```bash
# Export all archived tweets to JSON
uv run tweetxvault export json

# Export a specific collection
uv run tweetxvault export json --collection bookmarks
uv run tweetxvault export json --collection tweets

# Export to a specific path
uv run tweetxvault export json --out ~/exports/my-bookmarks.json
```

JSON exports now include normalized `media`, `urls`, and `article` sections alongside each exported tweet row.

### Media + URL Enrichment

```bash
# Download all pending archived media files into the local data dir
uv run tweetxvault media download

# Download only the next 100 pending media rows
uv run tweetxvault media download --limit 100

# Only download photos
uv run tweetxvault media download --photos-only

# Fetch final URL, canonical URL, title, and description metadata
uv run tweetxvault unfurl

# Unfurl only the next 100 saved URLs
uv run tweetxvault unfurl --limit 100

# Retry previously failed URL unfurls
uv run tweetxvault unfurl --retry-failed
```

### Gemini tagging

With tagging enabled in `config.toml`, the bare command processes eligible text and media tweets,
including directly quoted originals, using the same loop as the automatic sync follow-up. Text and
media candidates are sent in separate homogeneous requests. A text tweet that quotes media uses the
media prompt and quoted attachment, while the quoted original is tagged independently. Processing
continues until no work remains or the configured Gemini requests-per-day limit is reached:

```bash
# Tag all eligible tweets, respecting configured batching and RPD
uv run tweetxvault tag

# Process at most 5 batches using the configured batch size
uv run tweetxvault tag --limit 5

# Process at most 5 batches of up to 20 tweets each
uv run tweetxvault tag --batch 20 --limit 5

# Tag or re-tag one archived tweet by ID or status URL
uv run tweetxvault tag 2026531440414925307
uv run tweetxvault tag https://x.com/example/status/2026531440414925307

# Preview one validated result without saving its description or tags
uv run tweetxvault tag 2026531440414925307 --test

# Override the configured Gemini model for this run
uv run tweetxvault tag --model gemini-3.6-flash
```

`--batch N` sets the number of tweets in each batch for this invocation. `--limit N`
limits the number of batches, so `--batch 20 --limit 5` selects at most 100 tweets across
five batches. Without `--batch`, the command uses `tagging.limit` when `tagging.batch` is
enabled and otherwise processes one tweet per batch.

`--test` is limited to one tweet and prints the archived tweet context and normalized tags rather
than the raw model response. Media results also include a generated description; text-only results
do not. Test mode does not create, replace, or mark a tag row, but it does count real Gemini generation attempts toward
the local RPD counter. RPD usage is stored per model in the archive, resets at Pacific
midnight, and counts failed requests and retries as well as successful generations.

### Thread Expansion

```bash
# Expand archived tweets through TweetDetail to capture parents/context rows
uv run tweetxvault threads expand

# Expand only the next 100 queued thread targets
uv run tweetxvault threads expand --limit 100

# Strictly bound crawler expansion to N degrees of separation (default: 1)
uv run tweetxvault threads expand --max-linked-depth 1

# Expand a specific thread target by URL or ID
uv run tweetxvault threads expand https://x.com/dimitrispapail/status/2026531440414925307
uv run tweetxvault threads expand 2026531440414925307

# Re-fetch an explicit target even if it was already expanded before
uv run tweetxvault threads expand --refresh 2026531440414925307
```

**Thread Depth Limits:** Twitter's `TweetDetail` payload includes large swaths of surrounding reply trees. To prevent the thread crawler from snowballing into an infinite queue, tweetxvault builds an in-memory Breadth-First Search (BFS) graph over quoted-post relationships and linked X status URLs. The `--max-linked-depth` flag limits how many "degrees of separation" the crawler can stray from root bookmarks and likes. The default `1` expands posts directly quoted or linked by those roots; `0` disables all relationship traversal. Relations discovered by a `TweetDetail` request become eligible on the next sync rather than extending the active command's snapshot.

### Article Refresh

```bash
# Refresh preview-only archived article rows via TweetDetail
uv run tweetxvault articles refresh

# Refresh only the next 100 preview-only article rows
uv run tweetxvault articles refresh --limit 100

# Refresh every archived article row, not just preview-only ones
uv run tweetxvault articles refresh --all
```

### Maintenance

```bash
# Show archive totals, per-collection coverage, sync recency, and storage health
uv run tweetxvault stats

# Replace the summary with full storage segments, zero-count queues, and hidden availability rows
uv run tweetxvault stats --detailed

# Vacuum and optimize the SQLite database
uv run tweetxvault optimize

# Run a fast SQLite integrity diagnostic explicitly
uv run tweetxvault db check

# Run SQLite's more exhaustive integrity diagnostic
uv run tweetxvault db check --full

# Rebuild normalized tweet fields and secondary objects from stored raw JSON
uv run tweetxvault rehydrate

# Recover richer legacy tombstone rows; add --dry-run or --limit N as needed
uv run tweetxvault repair legacy-tombstones

# Explicitly include the potentially expensive timeline-capture scan
uv run tweetxvault repair legacy-tombstones --scan-timeline-captures

# Force-refresh query IDs from Twitter's JS bundles
uv run tweetxvault auth refresh-ids
```

`tweetxvault stats` and the Web statistics modal collect their values through the same typed
statistics service. Both expose overview and collection counts, archive/enrichment status,
detailed storage usage, tagging coverage, and frequent topics. The CLI presents those metrics as
a responsive Rich bento grid that pairs related tiles on wide terminals and stacks them on narrow
ones, while the Web modal retains its purpose-built browser layout. Pass `--detailed` to swap the
CLI's simplified storage categories for the full component breakdown and include zero-count
maintenance and unavailable-reason rows—the terminal equivalent of the Web modal's Detailed and
Show empty controls. Detailed storage lists posters, thumbnails, and similar supporting files
separately so they do not inflate photo/video counts. The tagging tile is omitted when the archive
has no generated tags. Legacy Web endpoints are thin adapters over the same collectors, so the two
surfaces share metric definitions without forcing them into the same visual design.

The Web modal keeps its complete statistics snapshot in the server process for five minutes.
Opening it again during that window serves the cached snapshot immediately. After five minutes,
the modal continues showing that snapshot while one background refresh recomputes the metrics;
the header shows when the displayed data was generated and offers a manual refresh button. Failed
refreshes leave the last successful snapshot available. `tweetxvault stats` never uses this Web
cache and always reads current archive data when invoked.

Tagging coverage uses the same base population as automatic tagging: directly saved text or media
posts with an available enriched tweet object, plus their directly quoted originals when those
objects are available. Quoted originals are counted once even when several saved posts quote them.
Other thread-only related posts and incomplete enrichment do not inflate the eligible total;
already-tagged posts remain in the denominator and count toward coverage when they contain a valid
nonempty tag list.

**Tombstones & Resurrection:** TweetDetail tombstones retain their original type, message, entities, and raw response while also receiving a stable reason such as `protected_account`, `suspended_account`, `account_missing`, `deleted_by_author`, or `unavailable_unknown`. A tombstone changes availability only when it can be positively associated with the requested tweet. X also sometimes returns an exact focal `TimelineTweet` entry with an empty `tweet_results` object; tweetxvault records that narrow sentinel as retryable `unavailable_unknown`. Missing focal entries and malformed nonempty result objects remain response-shape ambiguities, and three consecutive absences stop the worker before a broken parser or API shape can mass-classify rows. Confirmed archive deletions and deleted-by-author posts are never retried automatically. Each normal sync checks at most 200 due, retryable unavailable tweets with reason-weighted scheduling. A successful account-level recovery persists a few same-author probes as immediately due and can prioritize a small same-account burst, always inside the same 200-request budget. `tweetxvault stats` reports initial-enrichment completeness and resurrection eligibility separately.

Already-current SQLite databases open with a cheap schema-version read and do not run integrity scans, index setup, or legacy repair during ordinary sync stages. A pre-v3 database still receives a validated `archive.db.pre-schema-v3...bak` before one direct upgrade to the current schema, and prints the backup plus legacy-repair summary once. Run `tweetxvault db check` (or `--full`) when you explicitly want an integrity scan. Startup repair during a real legacy upgrade is deliberately bounded to indexed local candidates; use `tweetxvault repair legacy-tombstones --scan-timeline-captures` only when you want the deeper scan.

Long-running archive writers such as `sync`, `import enrich`, `threads expand`,
`articles refresh`, `media download`, and `unfurl` now do a best-effort compact
on the first `Ctrl-C` after substantial committed writes. Press `Ctrl-C` again
while that compact is running to skip it and exit; if you do, run
`tweetxvault optimize` later.

## Unattended sync via cron

```cron
# Sync bookmarks/likes plus the normal follow-up archive maintenance every 6 hours
0 */6 * * * cd /path/to/tweetxvault && uv run tweetxvault sync 2>> /tmp/tweetxvault.log
```

A process lock prevents overlapping runs.

When stdout/stderr is not a TTY—or when systemd's `INVOCATION_ID` or `JOURNAL_STREAM` is
present—tweetxvault automatically switches to plain service logs. These contain command and step
start/completion records, useful counters, retry/cooldown events, and bounded progress milestones,
but no spinners, progress-bar frames, ANSI control sequences, or continuously redrawn panels. This
keeps redirected cron logs and `journalctl` detailed enough to diagnose current work without
recording one line per item. Records use compact `Step: event · details` lines and avoid repeating
the command name, current-item activity, and completion counters when the step summary already
contains the same information.

## Configuration

Only the permanent `[auth]` skeleton is created automatically. Every other configuration section
and value is optional; missing values use built-in application defaults. The Web UI writes only
settings that you actually change. Resetting a setting removes its explicit override, and Advanced
settings remain absent from `config.toml` until customized. Environment-derived values affect only
the running process and are never copied into the file as a side effect.

The fixed SQLite defaults are a 512 MiB page-cache target per database connection and a 1 GiB mmap
request. Both can be explicitly overridden in `[database]` or through Advanced settings.

### Sync tuning (config.toml or env vars)

| Setting | Default | Env var |
|---------|---------|---------|
| `sync.page_delay` | `2.0` s | `TWEETXVAULT_PAGE_DELAY` |
| `sync.max_retries` | `3` | `TWEETXVAULT_MAX_RETRIES` |
| `sync.backoff_base` | `2.0` s | `TWEETXVAULT_BACKOFF_BASE` |
| `sync.detail_max_retries` | `2` | `TWEETXVAULT_DETAIL_MAX_RETRIES` |
| `sync.detail_backoff_base` | `30.0` s | `TWEETXVAULT_DETAIL_BACKOFF_BASE` |
| `sync.cooldown_threshold` | `3` consecutive 429s | `TWEETXVAULT_COOLDOWN_THRESHOLD` |
| `sync.cooldown_duration` | `300.0` s | `TWEETXVAULT_COOLDOWN_DURATION` |
| `sync.timeout` | `30.0` s | `TWEETXVAULT_TIMEOUT` |
| `sync.max_linked_depth` | `1` | `TWEETXVAULT_MAX_LINKED_DEPTH` |

## Data storage

Data paths are resolved by [platformdirs](https://platformdirs.readthedocs.io/) so they follow OS conventions, but the current runtime target is Unix-like systems only. On Linux the defaults are:

| Purpose | Default path |
|---------|-------------|
| Config | `~/.config/tweetxvault/` |
| Archive (SQLite) | `~/.local/share/tweetxvault/archive.db` |
| Cache (query IDs) | `~/.cache/tweetxvault/` |

Override with `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`.

## How it works

tweetxvault calls Twitter's internal GraphQL API — the same endpoints the web app uses. It:

1. Resolves session cookies (env/config/browser extraction)
2. Discovers current GraphQL query IDs by parsing Twitter's JS bundles (with a 24h TTL cache and static fallbacks)
3. Fetches timeline pages with the proper headers, feature flags, and cursor pagination
4. Stores raw API responses + collection tweet rows + normalized secondary objects in a local SQLite table
5. Tracks sync state per collection so the next run picks up where it left off

## Development

```bash
uv sync --extra web

# Run tests
uv run pytest

# Lint and format
uv run ruff check
uv run ruff format --check
```

See [`docs/`](docs/README.md) for architecture docs, the implementation plan, and research notes.

## Similar projects

- **[twitter-web-exporter](https://github.com/prinsss/twitter-web-exporter)** — Browser extension (Tampermonkey/Violentmonkey) that intercepts Twitter's GraphQL responses in-page; exports bookmarks, likes, tweets, followers, and DMs to JSON/CSV/HTML with bulk media download
- **[tweethoarder](https://github.com/tfriedel/tweethoarder)** — Python CLI archiver for likes, bookmarks, tweets, reposts, and home feed into SQLite with JSON/Markdown/CSV/HTML export
- **[Siftly](https://github.com/nichochar/Siftly)** — Self-hosted AI bookmark manager (Next.js + SQLite + Anthropic API) with entity extraction, vision analysis, and mindmap visualization
- **[TweetVault (helioLJ)](https://github.com/helioLJ/TweetVault)** — Self-hosted bookmark archive (Go + Next.js + PostgreSQL) with tag management; imports via twitter-web-exporter ZIP
- **[twitter-likes-export](https://github.com/gasser707/twitter-likes-export)** — Minimal Python scripts to export likes via Twitter's GraphQL API with optional media download
- **[download_twitter_likes](https://github.com/raviddog/download_twitter_likes)** — Playwright-based media downloader that scrolls your likes page and saves images/GIFs/videos

## License

Apache 2.0
