from email.message import EmailMessage

from ainews.credentials import clean_credential as _clean_credential
from ainews.sources.imap import _clean_folder, _decode, message_body


def build_newsletter() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Import AI <jack@importai.example>"
    msg["Subject"] = "Import AI 400: models, chips, policy"
    msg.set_content("Plain text fallback.")
    msg.add_alternative(
        "<html><body><p>This week: a new model shipped.</p>"
        '<a href="https://lab.example/model">Read more</a>'
        '<a href="https://lab.example/model">Read more again</a>'
        '<a href="mailto:x@y.z">mail</a>'
        "</body></html>",
        subtype="html",
    )
    return msg


def test_message_body_prefers_html_and_extracts_links():
    text, links = message_body(build_newsletter())
    assert "a new model shipped" in text
    assert links == ["https://lab.example/model"]


def test_message_body_falls_back_to_plain_text():
    msg = EmailMessage()
    msg.set_content("Just text, no HTML part.")
    text, links = message_body(msg)
    assert "Just text" in text
    assert links == []


def test_message_body_skips_attachments():
    msg = build_newsletter()
    msg.add_attachment(b"binary", maintype="application", subtype="pdf", filename="report.pdf")
    text, _ = message_body(msg)
    assert "binary" not in text


def test_decode_handles_encoded_headers():
    assert _decode("=?utf-8?q?Weekly_AI_Digest?=") == "Weekly AI Digest"
    assert _decode(None) == ""


# --- credential hygiene ------------------------------------------------------
#
# A Gmail App Password copied out of Google's UI carries U+00A0 non-breaking
# spaces between its four-character groups. imaplib encodes command arguments as
# ASCII, so the login raised UnicodeEncodeError before anything reached the
# server - and since a failing source is caught per-source, the only symptom was
# that newsletters silently never appeared. Seen in a real CI run:
#   UnicodeEncodeError: 'ascii' codec can't encode character '\xa0' in position 5


def test_a_pasted_gmail_app_password_is_usable():
    pasted = "abcd\xa0efgh\xa0ijkl\xa0mnop"
    cleaned = _clean_credential(pasted, "password", "newsletters")
    assert cleaned == "abcdefghijklmnop"
    cleaned.encode("ascii")  # what imaplib does; used to raise


def test_plain_spaces_in_a_pasted_password_go_too():
    assert _clean_credential("abcd efgh ijkl mnop", "password", "s") == "abcdefghijklmnop"


def test_a_clean_credential_is_untouched():
    assert _clean_credential("hunter2", "password", "s") == "hunter2"
    assert _clean_credential("me@example.com", "username", "s") == "me@example.com"


def test_surrounding_whitespace_from_a_secret_is_trimmed():
    """A trailing newline is easy to get into a CI secret."""
    assert _clean_credential("me@example.com\n", "username", "s") == "me@example.com"


def test_folder_names_keep_their_spaces():
    """Unlike a credential, "AI News" is a legitimate folder name."""
    assert _clean_folder("AI News") == "AI News"
    assert _clean_folder("  INBOX  ") == "INBOX"


def test_a_non_breaking_space_in_a_folder_becomes_a_real_space():
    """Same encoding error, but here the repair is a space, not deletion."""
    cleaned = _clean_folder("AI\xa0News")
    assert cleaned == "AI News"
    cleaned.encode("ascii")
