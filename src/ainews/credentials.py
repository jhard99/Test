"""Credential hygiene shared by the IMAP reader and the SMTP sender.

Both log in with the same kind of secret - typically the same Gmail App
Password - through stdlib clients that encode their protocol arguments as
ASCII. So both hit the identical failure on a pasted password, and both need
the identical repair. It lives here rather than in either module so fixing one
cannot leave the other broken, which is exactly what happened the first time.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def clean_credential(value: str, label: str, context: str) -> str:
    """Strip whitespace from a username or password.

    Gmail shows an App Password as four groups of four characters, and copying
    it out of that page brings the separators along - as U+00A0 non-breaking
    spaces, not plain ones. `imaplib` and `smtplib` both encode their arguments
    as ASCII, so a pasted password raises UnicodeEncodeError before anything
    reaches the server:

        UnicodeEncodeError: 'ascii' codec can't encode character '\\xa0'

    App passwords are meant to be entered without the spaces and mail clients
    strip them, so do the same. A genuine space inside a password is vanishingly
    rare next to this paste, but say when something was removed so that a login
    that changes behaviour is explainable.
    """
    cleaned = "".join(value.split())
    if cleaned != value:
        log.info(
            "%s: removed whitespace from the %s (a pasted Gmail App Password carries "
            "non-breaking spaces that cannot be sent over IMAP or SMTP)",
            context,
            label,
        )
    return cleaned
