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
import logging
import re
import smtplib
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import getaddresses
from typing import TYPE_CHECKING

from .base import Alert, NotifierError

_log = logging.getLogger("cairn.notify.smtp")

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
# Only CR/LF. Splitting on these BEFORE sanitizing keeps a smuggled newline from fusing two
# addresses into one malformed one. Commas are deliberately NOT split here — `getaddresses` is
# RFC-aware and respects quoting, so it separates `a@x, b@y` correctly while keeping a quoted
# display name like `"Team, Ops" <ops@x>` intact. Splitting on commas ourselves tore that apart and
# silently dropped a legitimate recipient.
_RECIPIENT_SPLIT = re.compile(r"[\r\n]+")


def _header_value(value: str) -> str:
    """Make ``value`` safe to interpolate into a mail header.

    Non-ASCII is deliberately left intact — ``EmailMessage`` RFC 2047-encodes it correctly, and
    mangling it would corrupt a legitimate collection name like ``Fotos Föhr``.
    """
    return _HEADER_CONTROLS.sub(" ", value).strip()


def _addresses(recipients: list[str]) -> list[Address]:
    """Parse ``recipients`` into real address objects, dropping anything unusable.

    Sanitizing each string and joining with ", " is not enough: recipients come from a
    panel-editable field, and a pasted value like ``"ops@example.com\\r\\nbackup@example.com"``
    would collapse into the single malformed ``"ops@example.com backup@example.com"``. The header
    would then be ambiguous, and since ``send_message`` derives the SMTP envelope from it, the mail
    could go nowhere while appearing to have been sent.

    So a recipient is first *split* on CR/LF — the smuggling vector — and the pieces are then
    parsed by ``getaddresses``, which handles comma separation and quoting per RFC. A piece is kept
    only if it is a real ``local@domain``. A bad piece is dropped with a warning rather than
    poisoning the whole header — one mistyped address must not cost the other recipients their
    alert, and equally, a *legitimate* address must never be dropped: a recipient silently removed
    here is a person who stops being told that their files changed.
    """
    candidates: list[str] = []
    for raw in recipients:
        for piece in _RECIPIENT_SPLIT.split(raw):
            # Semicolons separate addresses in Outlook-style pastes, but only split on them when
            # no quoting is in play — a quoted display name may legitimately contain one, and
            # tearing it apart would drop a real recipient.
            subpieces = piece.split(";") if '"' not in piece else [piece]
            for sub in subpieces:
                cleaned = _header_value(sub)
                if cleaned:
                    candidates.append(cleaned)

    out: list[Address] = []
    for display, addr in getaddresses(candidates):
        addr = addr.strip()
        # rpartition, so a quoted local part containing "@" splits at the real separator.
        local, sep, domain = addr.rpartition("@")
        if not sep or not local or not domain:
            _log.warning("dropping unusable alert recipient %r", addr)
            continue

        # A quoted local part is legal and may contain spaces and a second "@"
        # (`"odd name"@example.com`). Note it *before* unquoting, because the quotes are what make
        # the whitespace below legitimate.
        quoted_local = len(local) > 1 and local.startswith('"') and local.endswith('"')
        if quoted_local:
            # getaddresses returns the quotes; Address re-quotes as needed, so leaving them would
            # yield the double-quoted `""odd name""@example.com`.
            local = local[1:-1]
        elif local.count("@") or domain.count("@"):
            _log.warning("dropping unusable alert recipient %r", addr)
            continue

        # Whitespace otherwise signals a fused address (`a@x.com b@y.com`), which would make the
        # header ambiguous and the envelope wrong.
        if not quoted_local and any(ch.isspace() for ch in addr):
            _log.warning("dropping unusable alert recipient %r", addr)
            continue
        if any(ch.isspace() for ch in domain):
            _log.warning("dropping unusable alert recipient %r", addr)
            continue

        # Address quotes the display name properly, so keeping it is safe and friendlier.
        out.append(Address(display_name=_header_value(display), username=local, domain=domain))
    return out


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
        recipients = _addresses(self.recipients)
        if not recipients:
            # Nobody left to send to. Raise rather than hand smtplib an empty To: a mail with no
            # recipient is not a delivered alert, and dispatch must log it as the failure it is.
            raise NotifierError("no usable email recipients after validation")
        msg["To"] = recipients

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
