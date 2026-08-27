"""SMTP email channel — the implemented, active transport.

Composes an alert and sends it via ``smtplib`` run in a worker thread (so the scanner's event loop
is never blocked). Resend / AWS-SES are recognized by the settings UI but not yet wired; selecting
one raises a clear :class:`NotifierError` rather than silently dropping the alert.

Two shapes, deliberately: with a review link the message becomes ``multipart/alternative`` (a
complete plaintext part plus an HTML part carrying a clickable "Review in Cairn" action); **without**
one it stays a single ``text/plain`` part, byte-identical to what deployments that never configure
``public_url`` receive today. Unconditional multipart would be a silent change to every existing
deploy's mail for no gain.

Everything interpolated into the HTML part is escaped. Paths in particular are attacker-influenced —
anyone who can create a file in a watched directory chooses a string that Cairn then mails out — so a
path is data, never markup. The plaintext part is *not* escaped: it is not HTML.
"""

from __future__ import annotations

import asyncio
import html
import re
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING

from .base import Alert, NotifierError

if TYPE_CHECKING:
    from ..config import Settings

# Inline styles only: mail clients strip <style> blocks and external assets, and the proposal rules
# out a template/branding framework. Kept to a handful of declarations that degrade to plain text.
_BODY_STYLE = (
    "margin:0;padding:24px;background:#f4f4f5;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    "color:#18181b;line-height:1.5"
)
_CARD_STYLE = (
    "max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e4e4e7;"
    "border-radius:8px;padding:24px"
)
_BUTTON_STYLE = (
    "display:inline-block;padding:10px 18px;background:#18181b;color:#ffffff;"
    "text-decoration:none;border-radius:6px;font-weight:600"
)
_PATH_STYLE = "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px"
_MUTED_STYLE = "color:#71717a;font-size:13px"

# A run of C0 controls or DEL. `EmailMessage` refuses a header value containing CR or LF outright
# ("Header values may not contain linefeed or carriage return characters") — and it raises
# ValueError, not NotifierError, from `_build_message`, which sits OUTSIDE send()'s try. That
# escapes the notifier and is swallowed by dispatch's blanket except: logged at WARNING, email
# never sent, on the one channel actually in production. Collection names are operator-supplied
# and only `.strip()`ed on creation, so a stray newline in a name is reachable, and it would cost
# the operator the alert about their missing files. Collapse instead of refuse: a slightly odd
# subject line delivers, a raised header does not.
_HEADER_CONTROLS = re.compile(r"[\x00-\x1f\x7f]+")


def _header_value(value: str) -> str:
    """Make ``value`` safe to interpolate into a mail header.

    Non-ASCII is deliberately left intact — ``EmailMessage`` RFC 2047-encodes it correctly, and
    mangling it would corrupt a legitimate collection name like ``Fotos Föhr``.
    """
    return _HEADER_CONTROLS.sub(" ", value).strip()


class SmtpNotifier:
    name = "email"

    def __init__(self, *, recipients: list[str], settings: Settings) -> None:
        self.recipients = recipients
        self.settings = settings

    def _plaintext(self, alert: Alert) -> str:
        """The complete alert as plain text — a plaintext-only client must lose nothing."""
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
        if alert.url:
            lines.append(f"Review and acknowledge: {alert.url}")
        else:
            lines.append("Review and acknowledge in the Cairn panel.")
        return "\n".join(lines)

    def _html(self, alert: Alert, url: str) -> str:
        """The HTML alternative. Only ever built when a link exists (it is the part's whole point)."""
        name = html.escape(alert.collection_name)
        summary = html.escape(alert.summary)
        href = html.escape(url, quote=True)
        link_text = html.escape(url)

        parts = [
            "<html>",
            f'<body style="{_BODY_STYLE}">',
            f'<div style="{_CARD_STYLE}">',
            f"<p style=\"margin:0 0 16px\">Cairn detected <strong>{summary}</strong> "
            f"in collection &#39;{name}&#39;.</p>",
        ]
        if alert.detected_at is not None:
            detected = html.escape(alert.detected_at.isoformat())
            parts.append(f'<p style="margin:0 0 16px;{_MUTED_STYLE}">Detected at: {detected}</p>')
        if alert.paths:
            parts.append('<p style="margin:0 0 8px"><strong>Affected files:</strong></p>')
            items = "".join(
                f'<li style="margin:0 0 4px">{html.escape(p)}</li>' for p in alert.paths
            )
            parts.append(f'<ul style="margin:0 0 20px;padding-left:20px;{_PATH_STYLE}">{items}</ul>')
        else:
            parts.append(
                f'<p style="margin:0 0 20px;{_MUTED_STYLE}">(no file paths recorded)</p>'
            )
        parts.append(
            f'<p style="margin:0 0 16px"><a href="{href}" style="{_BUTTON_STYLE}">'
            "Review in Cairn</a></p>"
        )
        # Clients that suppress the anchor (or a reader who wants to see where it goes) still get
        # the address as text.
        parts.append(f'<p style="margin:0;{_MUTED_STYLE}">{link_text}</p>')
        parts.append("</div></body></html>")
        return "".join(parts)

    def _build_message(self, alert: Alert) -> EmailMessage:
        msg = EmailMessage()
        # Every interpolated value here is operator- or scanner-supplied and unvalidated, so all of
        # them go through _header_value: a header that cannot be built is a mail that is not sent.
        summary = _header_value(alert.summary)
        name = _header_value(alert.collection_name)
        msg["Subject"] = f"Cairn: {summary} in {name}"
        msg["From"] = _header_value(self.settings.smtp_from or "cairn@localhost")
        msg["To"] = ", ".join(_header_value(r) for r in self.recipients)

        msg.set_content(self._plaintext(alert))
        if alert.url:
            # Only now does the message become multipart/alternative; link-free mail stays single-part.
            msg.add_alternative(self._html(alert, alert.url), subtype="html")
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
