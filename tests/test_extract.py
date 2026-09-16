from ainews.extract import extract_article, extract_links, html_to_text, looks_paywalled

ARTICLE = """
<html><head><title>Fallback title</title>
<meta property="og:title" content="Lab ships new model"></head>
<body>
  <nav>Home About <a href="https://ads.example.com/x">Ad</a></nav>
  <script>var tracking = 1;</script>
  <article>
    <p>The lab released the model on Tuesday, describing it as its most capable system to date.</p>
    <p>Independent evaluations are not yet available, and pricing was not disclosed at launch.</p>
  </article>
  <footer>Copyright</footer>
</body></html>
"""


def test_extract_article_prefers_og_title_and_article_body():
    title, text = extract_article(ARTICLE)
    assert title == "Lab ships new model"
    assert "released the model on Tuesday" in text
    assert "tracking" not in text and "Copyright" not in text


def test_extract_article_handles_empty_input():
    assert extract_article("") == ("", "")
    assert html_to_text("") == ""


def test_html_to_text_drops_scripts_and_collapses_whitespace():
    text = html_to_text("<div><style>p{}</style><p>Hello   world</p>\n\n\n<p>Again</p></div>")
    assert "Hello world" in text
    assert "p{}" not in text
    assert "\n\n\n" not in text


def test_extract_links_dedupes_and_keeps_order():
    html = '<a href="https://a.com/1">a</a><a href="/relative">r</a><a href="https://a.com/1">dup</a><a href="https://b.com">b</a>'
    assert extract_links(html) == ["https://a.com/1", "https://b.com"]


def test_looks_paywalled_detects_stub_and_marker():
    assert looks_paywalled("")
    assert looks_paywalled("Two short sentences only.")
    stub = "word " * 200 + " Subscribe to continue reading this article."
    assert looks_paywalled(stub)
    assert not looks_paywalled("sentence " * 400)
