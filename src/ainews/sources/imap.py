"""Newsletter subscriptions, read straight from your inbox over IMAP.

This is how most "news subscriptions" actually arrive: Import AI, TLDR AI,
Ben's Bites, The Batch, Platformer and friends all land as email.
"""

from __future__ import annotations

import email
import imaplib
import logging
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from ainews.extract import extract_links, html_to_text
from ainews.models import Item
from ainews.sources.base import Source, register

log = logging.getLogger(__name__)

_MAX_BODY_CHARS = 60_000


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def message_body(msg: Message) -> tuple[str, list[str]]:
    """Return (plain text, outbound links) for an email, preferring text/html
    because newsletters put their links there."""
    html_parts: list[str] = []
    text_parts: list[str] = []

    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            decoded = payload.decode("utf-8", errors="replace")
        (html_parts if ctype == "text/html" else text_parts).append(decoded)

    if html_parts:
        html = "\n".join(html_parts)
        return html_to_text(html)[:_MAX_BODY_CHARS], extract_links(html)
    return "\n".join(text_parts)[:_MAX_BODY_CHARS], []


@register
class ImapSource(Source):
    """Options:
        host, port, username, password  (required; use an app password)
        folder:          mailbox to read, default INBOX
        senders:         only keep mail from these addresses/domains
        subject_include: only keep subjects containing one of these substrings
        unread_only:     restrict to UNSEEN messages (default false)
        mark_seen:       mark processed mail as read (default false)
    """

    type_name = "imap"
    required_options = ("host", "username", "password")

    def fetch(self, since: datetime) -> list[Item]:
        missing = self.missing_options()
        if missing:
            log.warning("%s: skipping, missing %s", self.name, ", ".join(missing))
            return []

        host = str(self.options["host"])
        port = int(self.options.get("port", 993))
        folder = str(self.options.get("folder", "INBOX"))
        senders = [s.lower() for s in self.options.get("senders", [])]
        subject_include = [s.lower() for s in self.options.get("subject_include", [])]
        mark_seen = bool(self.options.get("mark_seen", False))

        criteria = ["SINCE", since.strftime("%d-%b-%Y")]
        if self.options.get("unread_only"):
            criteria.append("UNSEEN")

        items: list[Item] = []
        try:
            with imaplib.IMAP4_SSL(host, port) as conn:
                conn.login(str(self.options["username"]), str(self.options["password"]))
                status, _ = conn.select(folder, readonly=not mark_seen)
                if status != "OK":
                    log.error("%s: cannot open folder %r", self.name, folder)
                    return []

                status, data = conn.search(None, *criteria)
                if status != "OK":
                    log.error("%s: IMAP search failed", self.name)
                    return []

                ids = data[0].split()
                # Newest first, capped: newsletters are long and we have a budget.
                for msg_id in reversed(ids[-(self.limit * 3) :]):
                    if len(items) >= self.limit:
                        break
                    item = self._fetch_one(conn, msg_id, since, senders, subject_include)
                    if item is not None:
                        items.append(item)
        except imaplib.IMAP4.error as exc:
            log.error("%s: IMAP error: %s", self.name, exc)
            return []
        except OSError as exc:
            log.error("%s: could not reach %s:%s (%s)", self.name, host, port, exc)
            return []

        return self.log_result(items)

    def _fetch_one(
        self,
        conn: imaplib.IMAP4_SSL,
        msg_id: bytes,
        since: datetime,
        senders: list[str],
        subject_include: list[str],
    ) -> Item | None:
        # BODY.PEEK leaves the \Seen flag alone unless the user opted in.
        fetch_spec = "(RFC822)" if self.options.get("mark_seen") else "(BODY.PEEK[])"
        status, data = conn.fetch(msg_id, fetch_spec)
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None

        msg = email.message_from_bytes(data[0][1])
        sender = _decode(msg.get("From"))
        subject = _decode(msg.get("Subject"))

        if senders and not any(s in sender.lower() for s in senders):
            return None
        if subject_include and not any(s in subject.lower() for s in subject_include):
            return None

        published: datetime | None = None
        raw_date = msg.get("Date")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                published = None
        if published is not None:
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < since:
                return None

        body, links = message_body(msg)
        if not body.strip():
            return None

        return Item(
            source=f"{self.name}: {sender.split('<')[0].strip() or sender}",
            source_type=self.type_name,
            title=subject or "(no subject)",
            url=links[0] if links else None,
            published=published,
            author=sender,
            summary=body[:600],
            text=body,
            links=links,
            extra={"newsletter": True, "from": sender},
        )
