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
| `reddit` | Top posts from r/MachineLearning, r/LocalLLaMA and friends, over Reddit's OAuth API. Needs a free registered app — see *Reddit* below. |
| `arxiv` | New cs.AI / cs.LG / cs.CL preprints. |
| `websearch` | **Open-web discovery** using Claude's server-side web search — finds stories none of your feeds carried. |

`ainews sources` lists the registered types. Adding a new one is a subclass of
`Source` plus a `@register` decorator in `src/ainews/sources/`.

**Don't want to use a terminal?** [SETUP-NO-TERMINAL.md](SETUP-NO-TERMINAL.md)
sets the whole thing up in the browser: GitHub runs it weekly on its own
machines and emails you the digest, with nothing installed locally.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .

cp config.example.yaml config.yaml     # edit the source list
cp env.example .env                    # fill in your secrets

ainews check                           # validate config + credentials
ainews check --probe                   # ...and actually fetch, to catch dead feeds
ainews fetch                           # what would be collected right now (no API calls)
ainews run --dry-run                   # write the digest, don't email it
ainews run                             # the real thing
```

`config.yaml` holds no secrets — only `${ENV_VAR}` references — so it is safe to
commit. Real values live in `.env` locally and in GitHub Actions secrets in CI.

### Commands

| Command | Purpose |
|---|---|
| `ainews run` | The full weekly pipeline. `--dry-run` skips email and state, `--no-llm` makes no API calls (keyword triage instead), `--include-seen` re-includes previously covered stories, `--window-days N` / `--since YYYY-MM-DD` change the window. |
| `ainews fetch` | List what the sources return right now. No Claude calls. |
| `ainews check` | Validate config, credentials and every source without fetching. `--probe` also fetches from each source and reports how many items it really returned, flagging dead (`EMPTY`) and frozen (`STALE`) feeds — see *When a feed dies*. |
| `ainews sources` | List available source types. |

`ainews run --no-llm` makes no API calls at all — see *Running without an Anthropic API key*.

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

## Reddit

Reddit's `robots.txt` is a blanket `Disallow: /` that covers its pages *and* the
`.json` views of them, backed by a stated Public Content Policy, so there is no
anonymous route a well-behaved client can take. The supported route is the OAuth
API, which is governed by Reddit's API terms instead, and it needs a free app:

1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) →
   *create another app…* → type **script**.
2. The client id is the string under the app's name; the secret is beside it.
3. Put them in `.env` as `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` (and as
   repository secrets for CI).

Leave them unset and the source sits the run out and says so in `ainews check`,
rather than quietly returning nothing.

## When a feed dies

Feeds rot in two ways, and both are silent: the URL starts 404ing, or it keeps
answering `200` while frozen. `ainews check` on its own only proves the config
parses, so `--probe` fetches from every source and reports what actually came
back:

```
$ ainews check --probe
sources (18), probed over 30 days:
  [      ok] WSJ Tech (paywalled) (37 items, newest 0d old)
  [   EMPTY] VentureBeat AI (rss) (nothing in 30 days - dead endpoint, moved feed, ...)
  [ MISSING] Reddit (reddit) (needs: client_id, client_secret)
  [ skipped] Web search (websearch) (needs model access; not probed)
```

The probe window (`--probe-days`, default 30) is deliberately wider than the
digest window: a source with nothing at all in a month is broken, not quiet. The
weekly workflow runs the probe on every run without ever failing on it, so a
feed that dies shows up in the job log instead of just thinning the digest.

Probing makes no API calls — model access is switched off for it, so it costs
nothing and the `websearch` source is reported as skipped rather than billed.

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
then delivers it four ways: **an issue on this repository** (needs no secrets —
this is what notifies you before SMTP is set up), an email if the SMTP secrets
exist, a build artifact, and a commit of the markdown to `digests/`. It also has
a manual `workflow_dispatch` trigger with `window_days`, `dry_run` and `no_llm`
inputs, and runs `ainews check --probe` on every run so a dead feed is visible
in the log.

`dry_run` skips both the email and the issue.

Add these repository secrets (Settings → Secrets and variables → Actions):

| Secret | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | required with `provider: anthropic`; see *Running without an Anthropic API key* for the alternatives |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` | newsletters |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | the reddit source; see *Reddit* above |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO` | delivery |
| `NEWS_COOKIES_B64` | optional; `base64 -w0 cookies.txt` for subscription full text |

The "already covered" database is carried between runs with `actions/cache`, so
a cache eviction costs you one week of possible repeats, nothing worse.

## Running without an Anthropic API key

Three supported routes, set with `llm.provider` in `config.yaml`:

| Situation | Setting | Credentials used |
|---|---|---|
| You have a Claude subscription but no API key | `provider: anthropic` | Run `ant auth login` once; the SDK picks up the profile automatically — no `ANTHROPIC_API_KEY` needed |
| Your org uses AWS | `provider: bedrock` | Your normal AWS credentials (env vars, profile or role) |
| Your org uses Google Cloud | `provider: vertex` + `vertex_project` | GCP application-default credentials (`gcloud auth application-default login`) |
| Your org uses Azure | `provider: foundry` + `foundry_resource` | Foundry credentials |
| No model access at all | `enabled: false` (or `ainews run --no-llm`) | None |

Two platform caveats the code handles for you: **Bedrock has no server-side web
search**, so the `websearch` source skips itself with a log line instead of
failing; **Vertex** supports only the basic web-search variant, which is
selected automatically.

### Using a Claude subscription (`ant auth login`)

If your organization doesn't allow API access, this is the route. One command,
no key, no `.env` entry:

```bash
ant auth login          # opens a browser; stores a profile in ~/.config/anthropic
ainews check            # should now print: credentials: ant profile 'default'
ainews run              # full synthesis
```

`build_client` constructs a bare `anthropic.Anthropic()`, and the SDK resolves
an `ant auth login` profile on its own — nothing else to configure.

> **The one trap.** A profile is consulted *only* when no API key is set, and
> **membership wins, not truthiness** — `ANTHROPIC_API_KEY=` (empty) or a
> leftover `sk-ant-...` placeholder in `.env` will beat a perfectly good profile
> and fail to authenticate. This is why `env.example` ships that line commented
> out. `ainews check` names whichever credential actually wins and warns when one
> is shadowing your profile.

**This does not extend to the scheduled GitHub Actions run.** A profile is a
short-lived OAuth token that the SDK refreshes from the credential file on your
disk; its refresh token also hard-expires rather than sliding with use. There is
no long-lived secret to hand CI, so a subscription login is a *local* capability.
Three ways to live with that:

| Want | Do |
|---|---|
| Synthesis, on a schedule, no API | Run the digest **locally** on a timer (macOS `launchd`, or `cron`) instead of in Actions |
| Keep using Actions | Let it run `--no-llm` (collection + keyword digest, no credentials), and run `ainews run` locally when you want the written version |
| Synthesis in CI | Needs a credential CI can hold: an API key, or Bedrock/Vertex — which bill through existing cloud spend and are often easier to get approved than a new vendor account |

Running an unattended weekly job off a personal subscription seat is also a
different thing from interactive use, so it's worth a glance at your own
organization's policy before wiring it to a scheduler.

### "Your organization is blocking new organization creation for domain …"

That error comes from Anthropic *signup*, not from the network: your employer or
university has claimed the email domain and disabled self-serve account
creation. Nothing is blocking this tool from reaching Claude. In rough order of
effort:

1. `ant auth login` — if you have any Claude subscription that works in Claude
   Code, the agent uses it with no API key. One command; `ainews check` will
   tell you whether it took.
2. Ask whoever administers your organization's Anthropic account for API access
   — that error usually means one already exists.
3. Sign up with a personal address for personal use (this tool reads public news
   and your own inbox; check your own policy if that changes).
4. Ask whether Claude is enabled in an AWS or GCP account you can use, and set
   `provider: bedrock` / `provider: vertex` — this bills through existing cloud
   spend, which is often easier to get approved than a new vendor account.
5. Run `llm.enabled: false` until one of the above lands.

### The zero-API mode

With `llm.enabled: false`, everything except the writing still happens —
collection, deduplication, cross-week suppression — and triage falls back to
keyword scoring: an AI-relevance test weighted toward headlines, an importance
score from event words, multi-outlet coverage, engagement and recency, topic
classification, and clustering by headline-token overlap. You get a filtered,
ranked digest grouped into sections with every story linked.

What you lose is the synthesis: no "why this matters", no merging of several
outlets' reporting into one account, and a cruder relevance call — it will
occasionally keep something dull or miss a story that never uses an obvious
keyword. It costs nothing and needs no credentials.

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
pytest            # 89 tests, no network required
```

Tests cover URL normalization and dedupe, config/env expansion, article
extraction, every source (with stubbed HTTP), IMAP message parsing, the triage
cache, cluster-assignment invariants, keyword triage and offline clustering,
provider selection, payload trimming, rendering and email assembly — plus the
robots.txt policy and its documented-API carve-out, and the guarantee that
`fetch` and `--no-llm` issue no API calls.

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
  heuristic.py    keyword triage + clustering for runs with no model access
  digest.py       writer prompt, payload assembly, fallback digest
  llm.py          Anthropic SDK wrapper (first-party, Bedrock, Vertex, Foundry)
  render.py       markdown + email HTML
  deliver.py      SMTP
  sources/        rss, paywalled, imap, hackernews, reddit, arxiv, websearch
```
