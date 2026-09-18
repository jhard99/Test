"""Send the digest by email."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from ainews.config import SmtpConfig
from ainews.credentials import clean_credential

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    pass


def build_message(cfg: SmtpConfig, subject: str, text_body: str, html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("AI Weekly", cfg.sender))
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or "localhost")
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(cfg: SmtpConfig, subject: str, text_body: str, html_body: str) -> None:
    if not cfg.configured:
        raise DeliveryError(
            "SMTP is not configured - set delivery.smtp.host, .sender and .recipients."
        )

    msg = build_message(cfg, subject, text_body, html_body)
    context = ssl.create_default_context()

    try:
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=60) as server:
                _login_and_send(server, cfg, msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as server:
                server.ehlo()
                if cfg.use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                _login_and_send(server, cfg, msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise DeliveryError(
            f"SMTP login rejected for {cfg.username!r}. For Gmail, use an App Password. ({exc.smtp_code})"
        ) from exc
    except smtplib.SMTPException as exc:
        raise DeliveryError(f"SMTP error talking to {cfg.host}:{cfg.port}: {exc}") from exc
    except UnicodeEncodeError as exc:
        # smtplib encodes the AUTH exchange as ASCII. Whitespace is stripped
        # above, so reaching here means some other non-ASCII character is in the
        # credentials - report it as a delivery failure rather than a traceback,
        # since by now the digest has been written and is worth keeping.
        raise DeliveryError(
            f"SMTP credentials for {cfg.username!r} contain a character that cannot be "
            f"sent ({exc.reason}: {exc.object[exc.start:exc.end]!r}). Retype them rather "
            "than pasting."
        ) from exc
    except OSError as exc:
        raise DeliveryError(f"Could not reach {cfg.host}:{cfg.port}: {exc}") from exc

    log.info("digest emailed to %s", ", ".join(cfg.recipients))


def _login_and_send(server: smtplib.SMTP, cfg: SmtpConfig, msg: EmailMessage) -> None:
    if cfg.username and cfg.password:
        # Same repair as the IMAP reader: a Gmail App Password pasted with its
        # non-breaking spaces cannot be ASCII-encoded, and smtplib raises while
        # building the AUTH response - after the digest has been written, so the
        # week's work is complete and then thrown away at the last step.
        server.login(
            clean_credential(cfg.username, "SMTP username", "delivery"),
            clean_credential(cfg.password, "SMTP password", "delivery"),
        )
    server.send_message(msg)
