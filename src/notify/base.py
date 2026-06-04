"""Notifier framework: the alert payload, the channel protocol, and the routing factory.

A channel's *credentials* (passwords, API keys) come from :class:`~src.config.Settings` (env), while
its per-collection *routing* (recipients, URLs, topics) lives in the collection's ``alert_json``. The
factory merges the two into ready-to-send notifier instances. An empty/``{}`` ``alert_json`` yields
no channels, so a collection with no configured alerts never touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import Settings
    from ..models.db import Collection


@dataclass
class Alert:
    """A single batched alert for one collection, reusable across channels and future triggers."""

    collection_name: str
    summary: str  # e.g. "2 missing, 1 modified"
    paths: list[str] = field(default_factory=list)  # affected relpaths (already capped)
    detected_at: datetime | None = None
    severity: str = "alert"


class NotifierError(Exception):
    """A channel failed to send (transport error, non-2xx, or misconfiguration)."""


@runtime_checkable
class Notifier(Protocol):
    """A notification channel. ``send`` raises :class:`NotifierError` on failure."""

    name: str

    async def send(self, alert: Alert) -> None:  # pragma: no cover - protocol signature
        ...


def build_channels(collection: Collection, settings: Settings) -> list[Notifier]:
    """Return the notifier instances enabled in ``collection.alert_json``.

    Disabled and unknown channels are skipped. Each channel merges its per-collection routing params
    with the matching global credentials from ``settings``. An empty/``{}`` config yields ``[]``.
    """
    from .kuma_push import KumaPushNotifier
    from .ntfy import NtfyNotifier
    from .signal_callmebot import SignalCallMeBotNotifier
    from .smtp import SmtpNotifier
    from .webhook import WebhookNotifier

    try:
        config = json.loads(collection.alert_json or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(config, dict):
        return []

    channels: list[Notifier] = []

    email = config.get("email")
    if isinstance(email, dict) and email.get("enabled"):
        recipients = [str(addr) for addr in (email.get("to") or []) if str(addr).strip()]
        if recipients:
            channels.append(SmtpNotifier(recipients=recipients, settings=settings))

    webhook = config.get("webhook")
    if isinstance(webhook, dict) and webhook.get("enabled"):
        url = (webhook.get("url") or "").strip()
        if url:
            channels.append(WebhookNotifier(url=url))

    ntfy = config.get("ntfy")
    if isinstance(ntfy, dict) and ntfy.get("enabled"):
        topic = (ntfy.get("topic") or "").strip()
        if topic:
            server = (ntfy.get("server") or "https://ntfy.sh").strip()
            channels.append(NtfyNotifier(topic=topic, server=server))

    signal = config.get("signal")
    if isinstance(signal, dict) and signal.get("enabled"):
        phone = (signal.get("phone") or "").strip()
        apikey = (signal.get("apikey") or "").strip()
        if phone and apikey:
            channels.append(SignalCallMeBotNotifier(phone=phone, apikey=apikey))

    kuma = config.get("kuma")
    if isinstance(kuma, dict) and kuma.get("enabled"):
        push_url = (kuma.get("push_url") or "").strip()
        if push_url:
            channels.append(KumaPushNotifier(push_url=push_url))

    return channels
