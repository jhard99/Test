from email.message import EmailMessage

from ainews.sources.imap import _decode, message_body


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
