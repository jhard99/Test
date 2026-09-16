"""Markdown -> file + email-ready HTML."""

from __future__ import annotations

from datetime import date

import markdown as markdown_lib

_CSS = """
body { margin: 0; padding: 24px 16px; background: #f6f7f9;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #1d2126; line-height: 1.55; }
.wrap { max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 10px;
        padding: 32px 36px; border: 1px solid #e3e6ea; }
h1 { font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 32px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #e3e6ea; }
h3 { font-size: 16px; margin: 22px 0 6px; }
p, li { font-size: 15px; }
ul { padding-left: 20px; }
li { margin: 5px 0; }
a { color: #1a5fb4; text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: #f0f2f4; padding: 1px 4px; border-radius: 3px; font-size: 13px; }
blockquote { margin: 12px 0; padding: 4px 14px; border-left: 3px solid #d7dbe0; color: #4a5058; }
.meta { color: #6b7280; font-size: 13px; margin: 0 0 8px; }
.footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #e3e6ea;
          color: #6b7280; font-size: 12px; }
"""


def title_for(start: date, end: date) -> str:
    return f"AI weekly · {start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"


def to_markdown(body: str, start: date, end: date, generated: str) -> str:
    """Full markdown document: title, date range, body, provenance footer."""
    return (
        f"# {title_for(start, end)}\n\n"
        f"_Covering {start.isoformat()} to {end.isoformat()}. Generated {generated}._\n\n"
        f"{body.strip()}\n"
    )


def to_html(markdown_text: str, footer: str = "") -> str:
    """Self-contained HTML with inline styles, safe for email clients."""
    html_body = markdown_lib.markdown(
        markdown_text, extensions=["extra", "sane_lists", "nl2br"]
    )
    footer_html = f'<div class="footer">{footer}</div>' if footer else ""
    return (
        "<!doctype html>\n<html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{html_body}{footer_html}</div></body></html>"
    )
