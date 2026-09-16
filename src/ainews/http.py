"""A deliberately polite HTTP client: identifies itself, rate-limits per host,
honours robots.txt, and only attaches subscription cookies to the domains you
explicitly allow."""

from __future__ import annotations

import http.cookiejar
import logging
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from ainews.config import HttpConfig

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, cfg: HttpConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self.cookie_domains = [d.lower().lstrip(".") for d in cfg.cookie_domains]
        self.authenticated_domains: set[str] = set()
        if cfg.cookies_file:
            self._load_cookies(Path(cfg.cookies_file))

    # --- cookies -------------------------------------------------------------

    def _load_cookies(self, path: Path) -> None:
        """Load a Netscape cookies.txt, keeping only cookies for allowed domains.

        Filtering matters: it means a full browser cookie export can't leak
        credentials for unrelated sites into requests this tool makes.
        """
        if not path.is_file():
            log.warning("cookies file %s not found - running without subscription access", path)
            return
        jar = http.cookiejar.MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (http.cookiejar.LoadError, OSError) as exc:
            log.warning("could not read cookies file %s: %s", path, exc)
            return

        kept = 0
        for cookie in jar:
            domain = (cookie.domain or "").lower().lstrip(".")
            match = next((d for d in self.cookie_domains if domain == d or domain.endswith("." + d)), None)
            if match is None:
                continue
            self.session.cookies.set_cookie(cookie)
            self.authenticated_domains.add(match)
            kept += 1
        log.info(
            "loaded %d cookies for %s",
            kept,
            ", ".join(sorted(self.authenticated_domains)) or "no allowed domain",
        )

    def has_credentials_for(self, domain: str) -> bool:
        domain = domain.lower().lstrip(".")
        return any(domain == d or domain.endswith("." + d) for d in self.authenticated_domains)

    # --- politeness ----------------------------------------------------------

    def _throttle(self, host: str) -> None:
        if self.cfg.requests_per_minute <= 0:
            return
        min_gap = 60.0 / self.cfg.requests_per_minute
        with self._lock:
            elapsed = time.monotonic() - self._last_request.get(host, 0.0)
            if elapsed < min_gap:
                time.sleep(min_gap - elapsed)
            self._last_request[host] = time.monotonic()

    def _robots_allows(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = self.session.get(f"{origin}/robots.txt", timeout=self.cfg.timeout)
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())  # type: ignore[union-attr]
                else:
                    parser = None  # no usable robots.txt -> treat as allow-all
            except requests.RequestException as exc:
                log.debug("robots.txt fetch failed for %s: %s", origin, exc)
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        if parser is None:
            return True
        return parser.can_fetch(self.cfg.user_agent, url)

    # --- requests ------------------------------------------------------------

    def get(self, url: str, **kwargs) -> requests.Response | None:
        """GET with throttling, robots check and retries. Returns None on failure."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            log.warning("refusing non-http(s) URL: %s", url)
            return None
        if not self._robots_allows(url):
            log.info("robots.txt disallows %s - skipping", url)
            return None

        timeout = kwargs.pop("timeout", self.cfg.timeout)
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.cfg.max_retries) + 1):
            self._throttle(parts.netloc)
            try:
                resp = self.session.get(url, timeout=timeout, allow_redirects=True, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                log.debug("GET %s failed (attempt %d): %s", url, attempt, exc)
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        time.sleep(min(int(retry_after), 60))
                else:
                    log.info("GET %s -> HTTP %d", url, resp.status_code)
                    return None
            if attempt < self.cfg.max_retries:
                time.sleep(min(2**attempt, 15))
        log.warning("giving up on %s: %s", url, last_error)
        return None

    def get_text(self, url: str) -> str:
        resp = self.get(url)
        if resp is None:
            return ""
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
            log.debug("skipping non-text response from %s (%s)", url, content_type)
            return ""
        return resp.text

    def close(self) -> None:
        self.session.close()
