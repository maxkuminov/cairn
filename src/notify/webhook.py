"""Webhook channel: POST a JSON alert payload to a configured URL (httpx, short timeout)."""

from __future__ import annotations

import httpx

from .base import Alert, NotifierError

_TIMEOUT = 10.0


class WebhookNotifier:
    name = "webhook"

    def __init__(self, *, url: str) -> None:
        self.url = url

    async def send(self, alert: Alert) -> None:
        payload = {
            "collection": alert.collection_name,
            "summary": alert.summary,
            "paths": alert.paths,
            "severity": alert.severity,
            "detected_at": alert.detected_at.isoformat() if alert.detected_at else None,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(self.url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifierError(f"webhook transport error: {exc}") from exc
        if resp.status_code >= 300:
            raise NotifierError(f"webhook returned HTTP {resp.status_code}")
