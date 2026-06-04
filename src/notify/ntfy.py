"""ntfy channel: POST the alert text to ``{server}/{topic}`` (httpx, short timeout)."""

from __future__ import annotations

import httpx

from .base import Alert, NotifierError

_TIMEOUT = 10.0


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
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
        except httpx.HTTPError as exc:
            raise NotifierError(f"ntfy transport error: {exc}") from exc
        if resp.status_code >= 300:
            raise NotifierError(f"ntfy returned HTTP {resp.status_code}")
