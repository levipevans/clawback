"""Host-side web fetch with SSRF guards.

The Docker sandbox stays offline. Only this host-side helper reaches the
network, and it refuses non-public addresses, credentials in URLs, non-http(s)
schemes, and redirects into private space.
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
USER_AGENT = "Clawback/0.1 (research; +https://github.com/levipevans/clawback)"
_PRIVATE_HOSTS = {"localhost", "localhost.localdomain"}


class WebError(RuntimeError):
    pass


def _addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebError(f"cannot resolve host: {host}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def is_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower()
    if host in _PRIVATE_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    try:
        addresses = _addresses(host, port)
    except WebError:
        return False
    if not addresses:
        return False
    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return False
    return True


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _Extractor()
    try:
        parser.feed(html)
    except Exception:
        return "", html
    return " ".join(parser.title.split()), parser.text()


def fetch_url(url: str, max_chars: int = 20_000) -> dict:
    if not is_public_url(url):
        raise WebError("refusing non-public or non-http(s) URL")
    max_chars = max(1_000, min(int(max_chars), 60_000))

    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
    ) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not is_public_url(current):
                raise WebError("refusing redirect to a non-public address")
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise WebError("redirect without a location")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise WebError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "")
                encoding = response.encoding or "utf-8"
                chunks: list[bytes] = []
                total = 0
                truncated = False
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        keep = max(0, MAX_BYTES - (total - len(chunk)))
                        chunks.append(chunk[:keep])
                        truncated = True
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
                text = body.decode(encoding, "replace")
                title = ""
                if "html" in content_type.lower() or text.lstrip().lower().startswith("<!doctype html"):
                    title, text = html_to_text(text)
                text = text.strip()
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n...[truncated]"
                    truncated = True
                return {
                    "ok": True,
                    "url": current,
                    "status": response.status_code,
                    "content_type": content_type,
                    "title": title,
                    "text": text,
                    "truncated": truncated,
                }
        raise WebError("too many redirects")
