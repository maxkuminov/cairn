"""User-facing strings and template wiring for the OIDC sign-in pages (oidc-sso-login, slice C).

`SSO_BUTTON_LABEL` is the ONE place the sign-in control's label is spelled: templates read it as
the Jinja global of the same name, tests assert against this constant, and nothing else in `src/`
writes the literal. Changing the label is a one-line edit here.
"""

from __future__ import annotations

from typing import Any

SSO_BUTTON_LABEL = "Sign in with SSO"

# Everything the auth templates read from the environment rather than from the route's context.
TEMPLATE_GLOBALS: dict[str, Any] = {"SSO_BUTTON_LABEL": SSO_BUTTON_LABEL}


def register_template_globals(templates: Any) -> None:
    """Expose `TEMPLATE_GLOBALS` on a `Jinja2Templates` instance (or a bare Jinja `Environment`).

    Idempotent. Whoever renders `login.html` must call this on the templates object it renders
    with; without it the label would render as an empty string.
    """
    env = getattr(templates, "env", templates)
    env.globals.update(TEMPLATE_GLOBALS)
