"""Uptime-Kuma push channel: GET the configured push URL (httpx, short timeout).

Optional legacy push heartbeat. Reports a degraded ``status=down`` with the alert summary as the
message so a Kuma monitor flips on a newly-detected change. ``msg`` is human-readable text, so
the review link is appended to it when one exists.
"""

from __future__ import annotations

import httpx

from .base import Alert, NotifierError

_TIMEOUT = 10.0


class KumaPushNotifier:
    name = "kuma"

    def __init__(self, *, push_url: str) -> None:
        self.push_url = push_url

    async def send(self, alert: Alert) -> None:
        msg = f"{alert.summary} in {alert.collection_name}"
        if alert.url:
            msg += f" - {alert.url}"
        params = {"status": "down", "msg": msg}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(self.push_url, params=params)
        except httpx.HTTPError as exc:
            raise NotifierError(f"Kuma push transport error: {exc}") from exc
        if resp.status_code >= 300:
            raise NotifierError(f"Kuma push returned HTTP {resp.status_code}")
