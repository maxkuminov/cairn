"""ntfy channel: POST the alert text to ``{server}/{topic}`` (httpx, short timeout).

When the alert carries a review link it goes in the body *and* in ntfy's native ``Click`` header, so
tapping the phone notification lands straight on the collection's review page.

The header is set defensively. ``normalize_public_url`` already guarantees a pure-ASCII URL, but a
value that cannot be encoded into an HTTP header raises inside httpx and would take out this
channel's delivery entirely — an ntfy-only operator would receive *nothing*. Losing the click target
is a cosmetic loss; losing the alert is the failure this whole product exists to prevent. So we check
rather than trust, and send the notification either way.
"""

from __future__ import annotations

import logging

import httpx

from .base import Alert, NotifierError

logger = logging.getLogger("cairn.notify.ntfy")

_TIMEOUT = 10.0


def _header_safe(value: str) -> bool:
    """True when ``value`` survives the ASCII encode httpx performs on outgoing header values."""
    try:
        value.encode("ascii")  # httpx: Headers normalize str values with encode("ascii")
    except UnicodeEncodeError:
        return False
    # CR/LF/NUL would be header injection; the rest of C0 and DEL are simply not valid in a header.
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, *, topic: str, server: str = "https://ntfy.sh") -> None:
        self.topic = topic
        self.server = server.rstrip("/")

    async def send(self, alert: Alert) -> None:
        url = f"{self.server}/{self.topic}"
        title = f"Cairn: {alert.summary} in {alert.collection_name}"
        body = "\n".join(alert.paths) if alert.paths else alert.summary
        headers = {"Title": title, "Priority": "high", "Tags": "warning"}
        if alert.url:
            body = f"{body}\n\n{alert.url}"  # the body is UTF-8 bytes, so it takes anything
            if _header_safe(alert.url):
                headers["Click"] = alert.url
            else:
                logger.warning("ntfy Click header omitted: review URL is not header-safe")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
        except httpx.HTTPError as exc:
            raise NotifierError(f"ntfy transport error: {exc}") from exc
        if resp.status_code >= 300:
            raise NotifierError(f"ntfy returned HTTP {resp.status_code}")
