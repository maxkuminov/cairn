"""ntfy channel: publish the alert as a JSON message to the ntfy server (httpx, short timeout).

When the alert carries a review link it goes in the body *and* in ntfy's native ``click`` field, so
tapping the phone notification lands straight on the collection's review page.

**Why JSON and not the header protocol.** ntfy's other form is ``POST {server}/{topic}`` with the
title, priority and click target as HTTP *headers*. httpx ASCII-encodes header values, so a
collection named ``Café`` — an ordinary name, not an attack — raised ``UnicodeEncodeError`` there.
That is not an ``httpx.HTTPError``, so it escaped ``send()`` and was swallowed by dispatch's blanket
except: logged at WARNING, notification never sent. A newline in a name was accepted into the header
verbatim. Every value ntfy needs is operator-supplied and unvalidated, so the fix is to stop putting
any of them in a header: the JSON body is UTF-8 and carries the real name through intact.

The remaining defensive check is on ``click`` alone, because that value is the one ntfy itself
validates as a URL. Losing the click target is a cosmetic loss; losing the alert is the failure this
whole product exists to prevent. So we check rather than trust, and send the notification either way.
"""

from __future__ import annotations

import logging

import httpx

from .base import Alert, NotifierError

logger = logging.getLogger("cairn.notify.ntfy")

_TIMEOUT = 10.0

# ntfy priorities are 1 (min) .. 5 (max); 4 is "high", which is what an integrity alert warrants.
_PRIORITY_HIGH = 4


def _click_safe(value: str) -> bool:
    """True when ``value`` is a plausible click target ntfy will accept.

    ``normalize_public_url`` already guarantees a pure-ASCII, control-free URL. This re-checks
    rather than trusts, so that a value which somehow arrived by another route omits the click
    target instead of risking the notification.
    """
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, *, topic: str, server: str = "https://ntfy.sh") -> None:
        self.topic = topic
        self.server = server.rstrip("/")

    async def send(self, alert: Alert) -> None:
        body = "\n".join(alert.paths) if alert.paths else alert.summary
        if alert.url:
            body = f"{body}\n\n{alert.url}"
        payload: dict[str, object] = {
            "topic": self.topic,
            "title": f"Cairn: {alert.summary} in {alert.collection_name}",
            "message": body,
            "priority": _PRIORITY_HIGH,
            "tags": ["warning"],
        }
        if alert.url:
            if _click_safe(alert.url):
                payload["click"] = alert.url
            else:
                logger.warning("ntfy click target omitted: review URL is not representable")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                # JSON publishing posts to the server root; the topic is a field, not a path.
                resp = await client.post(self.server or "/", json=payload)
        except httpx.HTTPError as exc:
            raise NotifierError(f"ntfy transport error: {exc}") from exc
        if resp.status_code >= 300:
            raise NotifierError(f"ntfy returned HTTP {resp.status_code}")
