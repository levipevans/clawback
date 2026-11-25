from __future__ import annotations

from clawback.web import html_to_text, is_public_url


def test_public_url_checks():
    assert is_public_url("https://example.com") is True
    assert is_public_url("http://127.0.0.1/") is False
    assert is_public_url("http://localhost/") is False
    assert is_public_url("http://10.0.0.1/") is False
    assert is_public_url("file:///etc/passwd") is False
    assert is_public_url("http://user:pass@example.com/") is False


def test_html_to_text_extracts_title_and_text():
    title, text = html_to_text(
        "<html><head><title>Hi</title><style>x</style></head>"
        "<body><h1>Hello</h1><p>World</p><script>bad()</script></body></html>"
    )
    assert title == "Hi"
    assert "Hello" in text
    assert "World" in text
    assert "bad()" not in text
