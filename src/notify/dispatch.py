"""Best-effort fan-out: send one alert to every enabled channel, isolating failures.

Each channel send is wrapped in its own try/except: a failure is logged and recorded as
``False`` in the result map, but never raises and never stops the other channels. This is the
boundary the scanner relies on — dispatch must never break a scan.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import Alert, build_channels

if TYPE_CHECKING:
    from ..config import Settings
    from ..models.db import Collection

_log = logging.getLogger("cairn.notify")


async def dispatch(alert: Alert, collection: Collection, settings: Settings) -> dict[str, bool]:
    """Send ``alert`` to every channel enabled for ``collection``. Returns ``{name: ok}``."""
    channels = build_channels(collection, settings)
    results: dict[str, bool] = {}
    for channel in channels:
        try:
            await channel.send(alert)
            results[channel.name] = True
        except Exception as exc:  # NotifierError or anything unexpected
            _log.warning(
                "alert channel %r failed for collection %r: %s",
                channel.name,
                alert.collection_name,
                exc,
            )
            results[channel.name] = False
    return results
