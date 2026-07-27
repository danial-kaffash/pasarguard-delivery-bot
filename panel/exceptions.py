"""Exception hierarchy for the PasarGuard panel client."""

from __future__ import annotations

from typing import Any


class PanelError(Exception):
    """Base class for all panel client errors."""


class PanelAuthError(PanelError):
    """Login failed, or the token was rejected and re-auth also failed."""


class PanelTransportError(PanelError):
    """Network-level failure (DNS, TLS, timeout, connection refused…)."""


class PanelAPIError(PanelError):
    """The panel returned an HTTP error status (>= 400)."""

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.method = method
        self.path = path
        super().__init__(
            f"panel API error {status_code} on {method or '?'} {path or '?'}: {detail}"
        )


class PanelNotFoundError(PanelAPIError):
    """404 — the requested resource (user, group…) does not exist."""


class PanelConflictError(PanelAPIError):
    """409 — e.g. a user with this username already exists."""
