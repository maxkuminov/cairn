"""Keep the OIDC callback's query out of uvicorn's access log (oidc-sso-login, design D11).

The authorization response arrives as ``GET /auth/oidc/callback?code=…&state=…&iss=…``, and
uvicorn's access logger writes the request line, query included, before and independently of any
route. Both entrypoints (``cairn serve`` → ``uvicorn.run`` and the image's ``uvicorn`` CMD) import
``src.main``, which installs this filter in ``oidc`` mode only, so neither depends on a launcher.

uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with ``(client, method, path_with_query, version,
status)``. The filter rewrites the request target of any path under ``/auth/`` (the callback, and
by extension every auth route) to the bare path, and **drops** a record whose arguments are not
that shape: a record it cannot sanitise is never passed through.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

ACCESS_LOGGER = "uvicorn.access"
_SENSITIVE_PREFIX = "/auth/"


class CallbackQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5 or not isinstance(args[2], str):
            return False
        target = args[2]
        path, sep, _query = target.partition("?")
        if sep and unquote(path).startswith(_SENSITIVE_PREFIX):
            record.args = (args[0], args[1], path, args[3], args[4])
        return True


def install() -> None:
    """Attach the filter to the ``uvicorn.access`` logger (idempotent)."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, CallbackQueryFilter) for f in logger.filters):
        logger.addFilter(CallbackQueryFilter())
