"""Export your own subscription cookies straight out of your browser.

The alternative is a browser extension, and every cookies.txt exporter needs
permission to read *all* your cookies - for every site, permanently. Reading the
browser's own cookie store locally avoids handing that to a third party, and it
only ever writes out the domains listed in `http.cookie_domains`.

Values are never logged or printed: the caller gets counts and names only.
"""

from __future__ import annotations

import http.cookiejar
import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: Browsers browser_cookie3 can read, as `ainews cookies --browser` values.
BROWSERS = ("chrome", "safari", "firefox", "edge", "brave", "chromium", "opera")

_MISSING = (
    "Reading browser cookies needs the optional browser-cookie3 package:\n"
    "    pip install 'ainews[cookies]'\n"
    "Or export a Netscape-format cookies.txt by hand and point "
    "NEWS_COOKIES_FILE at it instead."
)


class CookieExportError(RuntimeError):
    """Raised when the browser's cookie store could not be read."""


def _loader(browser: str):
    try:
        import browser_cookie3
    except ImportError as exc:
        raise CookieExportError(_MISSING) from exc

    try:
        return getattr(browser_cookie3, browser)
    except AttributeError as exc:
        raise CookieExportError(
            f"Unknown browser {browser!r}. Try one of: {', '.join(BROWSERS)}."
        ) from exc


def export_cookies(
    domains: list[str], out_path: Path, browser: str = "chrome"
) -> dict[str, list[str]]:
    """Write a Netscape cookies.txt holding only `domains`' cookies.

    Returns {domain: [cookie names]} for reporting. Raises CookieExportError if
    the browser's store can't be read at all; a domain with no cookies is not an
    error (you may simply not be signed in to it).
    """
    load = _loader(browser)
    jar = http.cookiejar.MozillaCookieJar(str(out_path))
    found: dict[str, list[str]] = {}

    for domain in domains:
        try:
            source = load(domain_name=domain)
        except Exception as exc:  # browser_cookie3 raises a wide variety
            raise CookieExportError(
                f"could not read {browser}'s cookies: {type(exc).__name__}: {exc}"
            ) from exc
        names = []
        for cookie in source:
            jar.set_cookie(cookie)
            names.append(cookie.name)
        found[domain] = sorted(set(names))

    # Session cookies have no expiry and are precisely the ones that carry a
    # login, so they must not be dropped on the way out.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)
    # Auth cookies on disk: readable by this user only.
    out_path.chmod(0o600)
    log.info(
        "wrote %s for %s",
        out_path,
        ", ".join(f"{d} ({len(n)})" for d, n in found.items()) or "no domains",
    )
    return found
