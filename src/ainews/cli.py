"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ainews import __version__, heuristic
from ainews.config import Config, ConfigError, load_dotenv
from ainews.deliver import DeliveryError, send_email
from ainews.digest import DigestInput, fallback_digest, plural, week_label, write_digest
from ainews.http import Fetcher
from ainews.llm import (
    LLMCredentialsError,
    LLMError,
    build_client,
    capabilities,
    credentials_source,
)
from ainews.pipeline import cluster, collect, dedupe, drop_seen, select, split_newsletters, triage
from ainews.render import title_for, to_html, to_markdown
from ainews.sources import available_types, build_source
from ainews.sources.base import Context
from ainews.store import Store

log = logging.getLogger("ainews")


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity < 0 else logging.INFO if verbosity == 0 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _since(args: argparse.Namespace, cfg: Config) -> datetime:
    if args.since:
        try:
            parsed = datetime.fromisoformat(args.since)
        except ValueError:
            raise SystemExit(f"--since must be ISO format (YYYY-MM-DD), got {args.since!r}")
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    days = args.window_days or cfg.window_days
    return datetime.now(timezone.utc) - timedelta(days=days)


def _context(cfg: Config) -> tuple[Context, Fetcher, Store]:
    fetcher = Fetcher(cfg.http)
    store = Store(cfg.state_db)
    return Context(config=cfg, fetcher=fetcher, store=store), fetcher, store


# --- commands ----------------------------------------------------------------


def _probe_source(
    source, since: datetime, cfg: Config, ok: bool, args: argparse.Namespace
) -> tuple[str, str, bool]:
    """Actually fetch from one source and describe what came back.

    `ainews check` on its own only proves the config parses. That is how a feed
    can 404 (Anthropic's) or freeze for twenty months (WSJ's old endpoint) while
    still reporting `[ok]` and contributing nothing. Probing over a window much
    wider than the digest's tells a genuinely quiet week apart from a dead
    endpoint: a source with nothing at all in `--probe-days` is broken, not
    quiet.
    """
    try:
        found = source.fetch(since)
    except Exception as exc:  # a broken source shouldn't abort the report
        return "ERROR", f" (fetch failed: {type(exc).__name__}: {exc})", False

    if not found:
        return (
            "EMPTY",
            f" (nothing in {args.probe_days} days - dead endpoint, moved feed, "
            "or an `include` filter that matches nothing?)",
            False,
        )

    newest = max((i.published for i in found if i.published), default=None)
    if newest is None:
        return "ok", f" ({len(found)} items, undated)", ok

    age_days = (datetime.now(timezone.utc) - newest).days
    detail = f" ({len(found)} items, newest {age_days}d old)"
    if age_days > cfg.window_days:
        return (
            "STALE",
            detail + f" - nothing inside the {cfg.window_days}-day digest window",
            False,
        )
    return "ok", detail, ok


def cmd_check(args: argparse.Namespace, cfg: Config) -> int:
    """Validate config and credentials without calling anything expensive."""
    ok = True
    print(f"config:      {args.config}")
    print(f"window:      {cfg.window_days} days")
    print(f"state db:    {cfg.state_db}")
    print(f"output dir:  {cfg.output_dir}")

    import os

    if not cfg.llm.enabled:
        print("\nmodels:      disabled (llm.enabled: false) - keyword triage, no API calls")
    else:
        print(f"\nmodels:      writer={cfg.llm.model}  triage={cfg.llm.triage_model}")
        print(f"provider:    {cfg.llm.provider}")
        caps = capabilities(cfg.llm.provider)
        if caps.note:
            print(f"             note: {caps.note}")
        if cfg.llm.provider == "anthropic":
            source, note = credentials_source()
            if source:
                print(f"credentials: {source}")
                if note:
                    print(f"             {note}")
            else:
                print(
                    "credentials: none found. Either run `ant auth login` to use a Claude\n"
                    "             subscription with no API key, or set ANTHROPIC_API_KEY; or set\n"
                    "             llm.provider to bedrock/vertex/foundry, or llm.enabled: false\n"
                    "             to run with keyword triage."
                )
        elif cfg.llm.provider == "bedrock":
            print(f"credentials: AWS default chain, region {cfg.llm.aws_region}")
        elif cfg.llm.provider == "vertex":
            print(f"credentials: GCP ADC, project {cfg.llm.vertex_project or '(unset!)'}, region {cfg.llm.vertex_region}")
        elif cfg.llm.provider == "foundry":
            print(f"credentials: Foundry resource {cfg.llm.foundry_resource or '(unset!)'}")

    if args.probe:
        # Probing does real fetches. Keep it free: with model access off, the
        # websearch source reports itself as skipped instead of billing for
        # searches, and every other source is plain HTTP.
        cfg.llm.enabled = False

    ctx, fetcher, store = _context(cfg)
    probe_since = datetime.now(timezone.utc) - timedelta(days=args.probe_days)
    try:
        header = f"\nsources ({len(cfg.sources)})"
        print(f"{header}, probed over {args.probe_days} days:" if args.probe else f"{header}:")
        for source_cfg in cfg.sources:
            status = "disabled" if not source_cfg.enabled else "ok"
            detail = ""
            source = None
            try:
                source = build_source(source_cfg, ctx)
                missing = source.missing_options()
                if source_cfg.enabled and missing:
                    status, ok = "MISSING", False
                    detail = f" (needs: {', '.join(missing)})"
            except ValueError as exc:
                status, ok = "ERROR", False
                detail = f" ({exc})"

            if args.probe and status == "ok" and source is not None:
                if source_cfg.type == "websearch":
                    status, detail = "skipped", " (needs model access; not probed)"
                else:
                    status, detail, ok = _probe_source(source, probe_since, cfg, ok, args)
            print(f"  [{status:>8}] {source_cfg.name} ({source_cfg.type}){detail}")

        if fetcher.authenticated_domains:
            print(f"\ncookies:     loaded for {', '.join(sorted(fetcher.authenticated_domains))}")
        elif cfg.http.cookie_domains:
            print(f"\ncookies:     none loaded (NEWS_COOKIES_FILE={cfg.http.cookies_file or 'unset'})"
                  f" - subscription sites will use abstracts only")

        if cfg.smtp.configured:
            print(f"delivery:    {cfg.smtp.host}:{cfg.smtp.port} -> {', '.join(cfg.smtp.recipients)}")
        else:
            print("delivery:    SMTP not configured - digests will only be written to disk")
    finally:
        store.close()
        fetcher.close()

    print("\nOK" if ok else "\nProblems found (see MISSING/ERROR above)")
    return 0 if ok else 1


def cmd_fetch(args: argparse.Namespace, cfg: Config) -> int:
    """Collect items and print them; no LLM calls, no email."""
    since = _since(args, cfg)
    # This command is documented as making no API calls, and one source (the
    # websearch one) reaches the API during collection - so model access has to
    # be switched off before collecting, not just around triage.
    cfg.llm.enabled = False
    ctx, fetcher, store = _context(cfg)
    try:
        items = dedupe(collect(cfg, ctx, since))
    finally:
        store.close()
        fetcher.close()

    items.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for item in items:
        when = item.published.date().isoformat() if item.published else "undated"
        print(f"{when}  {item.source[:28]:<28}  {item.title[:90]}")
        if args.verbose > 0 and item.url:
            print(f"{'':>12}  {item.url}")
    print(f"\n{len(items)} unique items since {since.date().isoformat()}")
    return 0


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    """The full weekly pipeline."""
    since = _since(args, cfg)
    end = datetime.now(timezone.utc)
    # Decided before collection: the websearch source calls the API while
    # collecting, so --no-llm has to reach it too, not only triage and writing.
    use_llm = cfg.llm.enabled and not args.no_llm
    cfg.llm.enabled = use_llm
    ctx, fetcher, store = _context(cfg)

    try:
        raw = collect(cfg, ctx, since)
        if not raw:
            log.error("no items collected - check your sources with `ainews check`")
            return 1

        items = dedupe(raw)
        stories, newsletters = split_newsletters(items)
        if not args.include_seen:
            stories = drop_seen(stories, ctx)
        log.info(
            "collected %d raw -> %d unique -> %d stories + %d newsletters",
            len(raw), len(items), len(stories), len(newsletters),
        )
        if not stories and not newsletters:
            # A whole quiet week in AI news means something is broken -
            # blocked feeds, expired credentials - not that nothing happened.
            log.error(
                "nothing new since the last digest; not sending an empty edition. "
                "Run `ainews fetch` to see what the sources return, or pass --include-seen."
            )
            return 1

        # Count contributing sources before dedupe, so an outlet whose copy of a
        # story lost the merge still counts as having covered the week.
        source_names = sorted({i.source for i in raw})

        if not use_llm:
            log.info("running without model access: keyword triage and clustering")
            clusters = heuristic.run(stories, cfg)
            log.info("keyword triage kept %d of %d items", sum(len(c.items) for c in clusters), len(stories))
        else:
            try:
                client = build_client(cfg.llm)
                scored = triage(stories, cfg, ctx, client)
            except LLMCredentialsError as exc:
                log.error("%s", exc)
                return 1
            kept = select(scored, cfg)
            log.info("triage kept %d of %d items", len(kept), len(scored))
            if not kept and not newsletters:
                log.error("nothing cleared the relevance bar this week")
                return 1
            clusters = cluster(kept, cfg, ctx, client)
            log.info("grouped into %d stories", len(clusters))

        data = DigestInput(
            clusters=clusters[: max(cfg.max_stories * 3, cfg.max_stories)],
            newsletters=newsletters[:12],
            start=since.date(),
            end=end.date(),
            total_candidates=len(raw),
            source_names=source_names,
        )

        if not use_llm:
            body = fallback_digest(data)
        else:
            try:
                body = write_digest(data, cfg, client)
            except LLMError as exc:
                log.error("%s - falling back to a plain listing", exc)
                body = fallback_digest(data)

        generated = end.strftime("%Y-%m-%d %H:%M UTC")
        markdown_text = to_markdown(body, data.start, data.end, generated)
        footer = (
            f"Assembled from {plural(len(source_names), 'source')} · "
            f"{plural(len(raw), 'candidate item')} · ainews {__version__}"
        )
        html = to_html(markdown_text, footer)

        out_dir = Path(args.out or cfg.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        label = week_label(data.end)
        md_path = out_dir / f"{label}.md"
        html_path = out_dir / f"{label}.html"
        md_path.write_text(markdown_text, encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")
        print(f"wrote {md_path}")
        print(f"wrote {html_path}")

        if args.dry_run:
            log.info("--dry-run: not sending email, not recording items as seen")
            return 0

        if cfg.smtp.configured:
            subject = cfg.smtp.subject_template.format(
                start=data.start.isoformat(), end=data.end.isoformat(), week=label
            )
            try:
                send_email(cfg.smtp, subject, markdown_text, html)
                print(f"emailed to {', '.join(cfg.smtp.recipients)}")
            except DeliveryError as exc:
                log.error("%s", exc)
                return 2
        else:
            log.warning("SMTP not configured - digest written to disk only")

        # Only record what the digest actually covered; anything we trimmed
        # stays eligible for next week.
        store.mark_seen([s.item for c in data.clusters for s in c.items], label)
        store.mark_seen(newsletters, label)
        store.purge()
        return 0
    finally:
        store.close()
        fetcher.close()


def cmd_sources(args: argparse.Namespace, cfg: Config) -> int:
    print("Available source types:")
    for name in available_types():
        print(f"  {name}")
    return 0


# --- argument parsing ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ainews",
        description="Collect the week's AI news from feeds, newsletters, forums and the "
        "open web, then write and email a digest.",
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="config file (default: config.yaml)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv file to load (default: .env)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="repeat for debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    parser.add_argument("--version", action="version", version=f"ainews {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_window(p: argparse.ArgumentParser) -> None:
        p.add_argument("--since", help="collect items published since this date (YYYY-MM-DD)")
        p.add_argument("--window-days", type=int, help="override the config window")

    p_run = sub.add_parser("run", help="collect, summarize, write and email the digest")
    add_window(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="write files but do not email or record state")
    p_run.add_argument(
        "--no-llm",
        action="store_true",
        help="make no API calls; use keyword triage and clustering instead",
    )
    p_run.add_argument("--include-seen", action="store_true", help="do not skip items from earlier digests")
    p_run.add_argument("--out", type=Path, help="output directory override")
    p_run.set_defaults(func=cmd_run)

    p_fetch = sub.add_parser("fetch", help="list what the sources return right now")
    add_window(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_check = sub.add_parser("check", help="validate config, credentials and sources")
    p_check.add_argument(
        "--probe",
        action="store_true",
        help="actually fetch from every source and report what came back, so a "
        "dead or frozen feed shows up as EMPTY/STALE instead of ok",
    )
    p_check.add_argument(
        "--probe-days",
        type=int,
        default=30,
        help="window for --probe (default 30). Wider than the digest window on "
        "purpose: a source with nothing in a month is broken, not quiet.",
    )
    p_check.set_defaults(func=cmd_check)

    p_sources = sub.add_parser("sources", help="list available source types")
    p_sources.set_defaults(func=cmd_sources)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(-1 if args.quiet else args.verbose)
    load_dotenv(args.env_file)

    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
