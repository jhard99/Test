# ainews — weekly AI news digest agent

Pulls the week's AI news from the open web, your RSS feeds, the newsletters in
your inbox, your subscription news sites, HN/Reddit/arXiv — then has Claude
triage, cluster and write a single digest and email it to you.

```
sources ──► dedupe ──► triage (Claude) ──► cluster (Claude) ──► write (Claude) ──► email + markdown/HTML
```

| Stage | What happens |
|---|---|
| **Collect** | Every configured source runs; a broken feed logs and is skipped, never fails the run. |
| **Dedupe** | URLs are normalized (tracking params stripped) so the same story from five outlets becomes one item that remembers who else carried it. Stories covered in an earlier digest are dropped via a SQLite ledger. |
| **Triage** | Claude scores each candidate: AI-related or not, importance 1–5, topic, one-line summary. Cached per item, so re-runs cost nothing. |
| **Cluster** | Claude groups items covering the same event so the digest doesn't repeat itself. |
| **Write** | Claude writes the digest from the collected text only, with a link on every story. |
| **Deliver** | Markdown + styled HTML on disk, emailed over SMTP. |

## Sources

| Type | What it reads |
|---|---|
| `rss` | Any RSS/Atom feed. `full_text: true` also fetches and extracts the article body. |
| `imap` | **Your newsletter subscriptions**, read straight from your inbox — Import AI, TLDR AI, Ben's Bites, The Batch, Platformer, any Substack. |
| `paywalled` | **Subscription sites** (NYT, WSJ, The Atlantic). Public feed by default; full text when you supply your own logged-in cookies (see below). |
| `hackernews` | Front-page-grade stories via the Algolia API, filtered by score. |
| `reddit` | Top posts from r/MachineLearning, r/LocalLLaMA and friends. |
| `arxiv` | New cs.AI / cs.LG / cs.CL preprints. |
| `websearch` | **Open-web discovery** using Claude's server-side web search — finds stories none of your feeds carried. |

`ainews sources` lists the registered types. Adding a new one is a subclass of
`Source` plus a `@register` decorator in `src/ainews/sources/`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .

cp config.example.yaml config.yaml     # edit the source list
cp env.example .env                    # fill in your secrets

ainews check                           # validate config + credentials
ainews fetch                           # what would be collected right now (no API calls)
ainews run --dry-run                   # write the digest, don't email it
ainews run                             # the real thing
```

`config.yaml` holds no secrets — only `${ENV_VAR}` references — so it is safe to
commit. Real values live in `.env` locally and in GitHub Actions secrets in CI.

### Commands

| Command | Purpose |
|---|---|
| `ainews run` | The full weekly pipeline. `--dry-run` skips email and state, `--no-llm` emits a plain listing with no API calls, `--include-seen` re-includes previously covered stories, `--window-days N` / `--since YYYY-MM-DD` change the window. |
| `ainews fetch` | List what the sources return right now. No Claude calls. |
| `ainews check` | Validate config, credentials and every source without fetching. |
| `ainews sources` | List available source types. |

## Connecting your newsletters (IMAP)

Newsletters are where most "news subscriptions" actually live, so they get a
lane of their own in the digest.

1. In Gmail, turn on 2FA and create an [App Password](https://myaccount.google.com/apppasswords).
   Your normal password will not work over IMAP.
2. Put it in `.env` as `IMAP_PASSWORD`.
3. Best results: make a Gmail filter that labels AI newsletters into their own
   label (say `AI News`), set `IMAP_FOLDER=AI News`, and delete the `senders:`
   list from the config so everything in that label is read.

The source reads with `BODY.PEEK` by default, so your unread counts are left
alone unless you set `mark_seen: true`.

## Subscription sites (NYT / WSJ / The Atlantic)

By default these run off each publisher's public RSS feed: headline, abstract
and link. That is enough for the digest to tell you what happened and where to
read it, and it needs no credentials.

For full article text, export a Netscape-format `cookies.txt` from the browser
profile where you are already signed in to your subscription and point
`NEWS_COOKIES_FILE` at it. Then:

- only the domains under `http.cookie_domains` receive cookies — every other
  cookie in the file is discarded when it loads, so a full browser export can't
  leak credentials to unrelated sites;
- requests stay inside `robots.txt` and the configured per-host rate limit;
- article text is used to write a digest for you and is never republished.

This reuses your own subscription for your own reading. It is not a paywall
bypass, and there is deliberately no support for one — no user-agent spoofing,
no archive mirrors. If a page stays paywalled, the item is marked and the digest
says so. Check your publishers' terms before using it for anything beyond
personal use.

## Scheduling (GitHub Actions)

`.github/workflows/weekly-digest.yml` runs the digest every Monday at 13:00 UTC,
emails it, uploads it as an artifact and commits the markdown to `digests/`.
It also has a manual `workflow_dispatch` trigger with `window_days` and
`dry_run` inputs.

Add these repository secrets (Settings → Secrets and variables → Actions):

| Secret | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | required |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` | newsletters |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | delivery |
| `NEWS_COOKIES_B64` | optional; `base64 -w0 cookies.txt` for subscription full text |

The "already covered" database is carried between runs with `actions/cache`, so
a cache eviction costs you one week of possible repeats, nothing worse.

## Models and cost

Both stages default to `claude-opus-5`, set in `config.yaml`:

```yaml
llm:
  model: claude-opus-5         # writes the digest - one call per week
  triage_model: claude-opus-5  # triage + clustering - scales with item count
  triage_effort: low
  writer_effort: high
```

Triage is where the volume is (one call per ~40 candidate items). If a run is
more expensive than you want, set `triage_model: claude-sonnet-5` or
`claude-haiku-4-5` and leave the writer on Opus — that's the single biggest
lever. Verdicts are cached in SQLite, so re-running a week costs almost nothing.
Other knobs: `max_items_per_source`, `min_importance` (raise it to 3 for a
tighter digest), `per_item_chars`, `max_stories`.

## Accuracy

The writer is instructed to use only the collected text, to link every story,
and to say so when it only saw an abstract or a paywall stub. It can still get
things wrong the way any summarizer can — the links are there so you can check.
If a Claude call fails, the run falls back to a plain linked listing rather than
producing nothing.

## Development

```bash
pip install -e ".[dev]"
pytest            # 48 tests, no network required
```

Tests cover URL normalization and dedupe, config/env expansion, article
extraction, every source (with stubbed HTTP), IMAP message parsing, the triage
cache, cluster-assignment invariants, payload trimming, rendering and email
assembly.

## Layout

```
src/ainews/
  cli.py          commands: run / fetch / check / sources
  config.py       YAML + ${ENV} expansion
  models.py       Item, ScoredItem, Cluster, URL normalization
  http.py         rate-limited, robots-aware fetcher with scoped cookies
  extract.py      HTML -> article text
  store.py        SQLite: seen-items ledger + triage/article cache
  pipeline.py     collect -> dedupe -> triage -> cluster
  digest.py       writer prompt, payload assembly, fallback digest
  llm.py          Anthropic SDK wrapper
  render.py       markdown + email HTML
  deliver.py      SMTP
  sources/        rss, paywalled, imap, hackernews, reddit, arxiv, websearch
```
