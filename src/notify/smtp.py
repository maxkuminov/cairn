"""SMTP email channel — the implemented, active transport.

Composes a plaintext alert and sends it via ``smtplib`` run in a worker thread (so the scanner's
event loop is never blocked). Resend / AWS-SES are recognized by the settings UI but not yet wired;
selecting one raises a clear :class:`NotifierError` rather than silently dropping the alert.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING

from .base import Alert, NotifierError

if TYPE_CHECKING:
    from ..config import Settings


class SmtpNotifier:
    name = "email"

    def __init__(self, *, recipients: list[str], settings: Settings) -> None:
        self.recipients = recipients
        self.settings = settings

    def _build_message(self, alert: Alert) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"Cairn: {alert.summary} in {alert.collection_name}"
        msg["From"] = self.settings.smtp_from or "cairn@localhost"
        msg["To"] = ", ".join(self.recipients)

        lines = [
            f"Cairn detected {alert.summary} in collection '{alert.collection_name}'.",
            "",
        ]
        if alert.detected_at is not None:
            lines.append(f"Detected at: {alert.detected_at.isoformat()}")
            lines.append("")
        if alert.paths:
            lines.append("Affected files:")
            lines.extend(f"  - {p}" for p in alert.paths)
        else:
            lines.append("(no file paths recorded)")
        lines.append("")
        lines.append("Review and acknowledge in the Cairn panel.")
        msg.set_content("\n".join(lines))
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        s = self.settings
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as client:
            if s.smtp_starttls:
                client.starttls()
            if s.smtp_user and s.smtp_password:
                client.login(s.smtp_user, s.smtp_password)
            client.send_message(msg)

    async def send(self, alert: Alert) -> None:
        provider = self.settings.email_provider
        if provider in ("resend", "ses"):
            raise NotifierError(f"{provider} transport not yet wired; use Local SMTP")
        if not self.settings.smtp_host:
            raise NotifierError("SMTP host is not configured (CAIRN_SMTP_HOST)")
        if not self.recipients:
            raise NotifierError("no email recipients configured for this collection")
        msg = self._build_message(alert)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except NotifierError:
            raise
        except Exception as exc:  # smtplib / OSError
            raise NotifierError(f"SMTP send failed: {exc}") from exc
