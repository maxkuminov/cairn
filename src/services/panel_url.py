"""The panel's public address: one grammar, one link builder.

Cairn puts its own URL into places that are unforgiving about what a string may contain — an HTML
``href`` in an alert email, ntfy's ``Click`` HTTP header, a webhook JSON payload. A URL that is
merely "absolute enough" is not safe there: a non-ASCII character raises when the value is encoded
into a header (silently costing an ntfy-only operator every alert), a quote breaks out of an
attribute, userinfo/query/fragment quietly change where the link points.

So the grammar lives here, in one place, and every link is built by :func:`panel_link` — never by
string concatenation at a call site. :func:`normalize_public_url` is deliberately strict and
*raising*; callers choose the failure mode. Config load is fail-soft (a typo must never stop a
scan), the panel save boundary is fail-loud (a human is there to read the error).

This module imports nothing from the rest of the app: it is a leaf, safe to import from
``src.config``.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

# Characters that are never legal in a URI *and* that break the contexts we embed the URL into:
# quotes/angle brackets escape an HTML attribute or tag, a backslash is re-read as a slash by
# browsers (so it can redirect the link), and the rest are RFC 3986 "unwise" delimiters. Rejecting
# beats percent-encoding here — an operator who typed one of these mistyped the address, and a
# silently-mangled link is worse than a refused one.
_FORBIDDEN_CHARS = frozenset('"\'<>`\\^{}|')

# Sub-delims + unreserved + "/:@", plus "%" so existing percent-escapes are not double-encoded.
_PATH_SAFE = "/%:@-._~!$&()*+,;="


def normalize_public_url(value: str | None) -> str | None:
    """Validate and canonicalize the panel's public base URL.

    Returns ``None`` for ``None``/empty/whitespace-only input (unset is a legitimate state, not an
    error). Otherwise returns a pure-ASCII, trailing-slash-free base URL, or raises ``ValueError``
    with a short human-readable reason — the panel shows that reason to an admin verbatim.

    Accepts ``http``/``https``, a non-empty host, an optional port, and an optional path prefix
    (``https://example.com/cairn`` for a sub-path reverse proxy). Rejects any other scheme,
    userinfo, a query string, a fragment, and ASCII control characters or whitespace.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Interior whitespace and control characters corrupt every embedding context (header injection
    # via CR/LF above all), and none of them can occur in a URL an operator meant to type.
    for ch in raw:
        if ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError("must not contain whitespace or control characters")
        if ch in _FORBIDDEN_CHARS:
            raise ValueError(f"must not contain {ch!r}")

    parsed = urlsplit(raw)

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("must start with http:// or https://")
    if parsed.query:
        raise ValueError("must not contain a query string")
    if parsed.fragment:
        raise ValueError("must not contain a '#' fragment")
    # Credentials in a link that gets mailed out are both a leak and a phishing shape.
    if "@" in parsed.netloc:
        raise ValueError("must not contain a username or password")

    host = parsed.hostname  # already lowercased; IPv6 literals come back without their brackets
    if not host:
        raise ValueError("must include a host, e.g. https://cairn.example.com")

    try:
        port = parsed.port
    except ValueError as exc:  # non-numeric or out-of-range port
        raise ValueError(f"invalid port ({exc})") from exc

    # Pure ASCII or nothing: the value is written verbatim into an HTTP header, and a non-ASCII
    # header value raises at encode time, which would take that channel's delivery out entirely.
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"host cannot be IDNA-encoded to ASCII ({exc})") from exc

    host_part = f"[{host}]" if ":" in host else host  # re-bracket an IPv6 literal
    netloc = host_part if port is None else f"{host_part}:{port}"

    path = parsed.path
    if not path.isascii():
        path = quote(path, safe=_PATH_SAFE)
        if not path.isascii():  # belt and braces — quote() should never leave non-ASCII behind
            raise ValueError("path cannot be percent-encoded to ASCII")

    normalized = f"{scheme}://{netloc}{path}".rstrip("/")
    if not normalized.isascii():
        raise ValueError("must be a pure-ASCII URL")
    return normalized


def panel_link(public_url: str | None, path: str) -> str | None:
    """Join the configured base URL to ``path`` with exactly one slash.

    ``None`` when ``public_url`` is unset — callers render a link-free message rather than guessing
    an address. A base with a path prefix keeps it:
    ``panel_link("https://x/cairn", "/collection/1/review")`` → ``https://x/cairn/collection/1/review``.
    This is the only place links are built.
    """
    if not public_url:
        return None
    base = public_url.rstrip("/")
    suffix = path.lstrip("/")
    return f"{base}/{suffix}" if suffix else base
