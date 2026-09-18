"""SMTP login must survive a pasted Gmail App Password.

The IMAP reader was fixed for this first; the SMTP sender uses the same secret
through the same kind of ASCII-encoding stdlib client and was missed, so a real
run built the whole digest and then died on the last step:

    UnicodeEncodeError: 'ascii' codec can't encode character '\\xa0' in
    position 29     (smtplib.auth, via deliver.py)

Both now share ainews.credentials.clean_credential, so the two cannot drift.
"""

import smtplib

import pytest

from ainews.config import SmtpConfig
from ainews.credentials import clean_credential
from ainews.deliver import DeliveryError, _login_and_send, send_email

PASTED = "abcd\xa0efgh\xa0ijkl\xa0mnop"


class FakeServer:
    def __init__(self, explode=None):
        self.explode = explode
        self.logins = []
        self.sent = []

    def login(self, user, password):
        self.logins.append((user, password))
        # What smtplib really does while building the AUTH response.
        f"\0{user}\0{password}".encode("ascii")
        if self.explode:
            raise self.explode

    def send_message(self, msg):
        self.sent.append(msg)


def _cfg(**over):
    base = dict(
        host="smtp.example.com",
        port=587,
        username="me@example.com",
        password=PASTED,
        sender="me@example.com",
        recipients=["me@example.com"],
    )
    base.update(over)
    return SmtpConfig(**base)


def test_a_pasted_app_password_can_be_sent():
    server = FakeServer()
    _login_and_send(server, _cfg(), msg="message")
    user, password = server.logins[0]
    assert password == "abcdefghijklmnop"
    assert server.sent == ["message"]


def test_the_same_helper_backs_imap_and_smtp():
    """Regression guard for the actual mistake: fixing one call site and
    leaving its sibling broken."""
    assert clean_credential(PASTED, "password", "x") == "abcdefghijklmnop"
    server = FakeServer()
    _login_and_send(server, _cfg(), msg="m")
    assert server.logins[0][1] == clean_credential(PASTED, "password", "x")


def test_a_clean_password_is_passed_through_untouched():
    server = FakeServer()
    _login_and_send(server, _cfg(password="hunter2"), msg="m")
    assert server.logins[0] == ("me@example.com", "hunter2")


def test_login_is_skipped_when_no_credentials_are_set():
    server = FakeServer()
    _login_and_send(server, _cfg(username="", password=""), msg="m")
    assert server.logins == []
    assert server.sent == ["m"]


def test_an_unsendable_credential_is_a_delivery_error_not_a_traceback(monkeypatch):
    """Whitespace is handled, so anything left is a real non-ASCII character.
    It must not escape as a bare UnicodeEncodeError: by this point the digest
    has been written, and cmd_run turns DeliveryError into a clean exit 2."""

    class Exploding:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            "café".encode("ascii")  # raises UnicodeEncodeError

        def send_message(self, msg):
            pass

    monkeypatch.setattr(smtplib, "SMTP", Exploding)
    with pytest.raises(DeliveryError, match="cannot be sent"):
        send_email(_cfg(password="caf\xe9"), "subject", "text", "<p>html</p>")
