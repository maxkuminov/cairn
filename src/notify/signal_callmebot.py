"""Signal channel via the CallMeBot relay: GET its API with the message (httpx, short timeout)."""

from __future__ import annotations

import httpx

from .base import Alert, NotifierError

_TIMEOUT = 10.0
_API = "https://api.callmebot.com/signal/send.php"


class SignalCallMeBotNotifier:
    name = "signal"

    def __init__(self, *, phone: str, apikey: str) -> None:
        self.phone = phone
        self.apikey = apikey

    async def send(self, alert: Alert) -> None:
        text = f"Cairn: {alert.summary} in {alert.collection_name}"
        if alert.paths:
            text += "\n" + "\n".join(alert.paths)
        params = {"phone": self.phone, "apikey": self.apikey, "text": text}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(_API, params=params)
        except httpx.HTTPError as exc:
            raise NotifierError(f"CallMeBot transport error: {exc}") from exc
        if resp.status_code >= 300:
            raise NotifierError(f"CallMeBot returned HTTP {resp.status_code}")
