"""Manages multiple PasarGuardApiClient instances, keyed by panel id.

One PasarGuard panel may exist per PasarGuard server.  The manager lazily
creates clients on first use, caches them, and invalidates when a panel's
credentials are updated or the panel is soft-deleted.
"""

from __future__ import annotations

import logging
import time

from storage.db import Panel

from .client import PasarGuardApiClient
from .models import GroupSimple

logger = logging.getLogger(__name__)

_GROUPS_CACHE_TTL = 300.0  # seconds


class PanelManager:
    """Lazily-created, cached panel clients."""

    def __init__(self) -> None:
        # panel_id → client
        self._clients: dict[int, PasarGuardApiClient] = {}
        # panel_id → (monotonic_ts, {group_id: group_name})
        self._groups_cache: dict[int, tuple[float, dict[int, str]]] = {}

    # ── client lifecycle ────────────────────────────────────────────────────

    def get_client(self, panel: Panel) -> PasarGuardApiClient:
        """Return (or lazily create) the client for a panel.

        If the client already exists, it is reused (the bearer token is cached
        inside it).  Call ``invalidate`` after a password change.
        """
        client = self._clients.get(panel.id)
        if client is None:
            client = PasarGuardApiClient(
                panel.base_url,
                panel.admin_username,
                panel.admin_password,
                verify_ssl=panel.verify_ssl,
                timeout=panel.timeout_seconds,
            )
            self._clients[panel.id] = client
            logger.debug("Created panel client for %r (id=%s)", panel.name, panel.id)
        return client

    def invalidate(self, panel_id: int) -> None:
        """Drop the cached client for a panel (e.g. after password change).

        The next call to ``get_client`` will create a fresh one.
        """
        client = self._clients.pop(panel_id, None)
        if client is not None:
            # We can't await aclose() here (sync context), but the client will
            # be GC'd.  If precise cleanup matters, callers can await
            # client.aclose() before calling invalidate().
            logger.debug("Invalidated panel client for id=%s", panel_id)
        self._groups_cache.pop(panel_id, None)

    async def close_all(self) -> None:
        """Close all cached clients (call during shutdown)."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self._groups_cache.clear()

    # ── groups cache (per-panel) ───────────────────────────────────────────

    async def list_groups(self, panel: Panel, *, force: bool = False) -> dict[int, str]:
        """GET /api/groups/simple with a per-panel 5-minute cache.

        Returns ``{group_id: group_name}``.
        """
        if not force:
            cached = self._groups_cache.get(panel.id)
            if cached and (time.monotonic() - cached[0]) < _GROUPS_CACHE_TTL:
                return cached[1]

        client = self.get_client(panel)
        groups: list[GroupSimple] = await client.list_groups_simple()
        mapping = {g.id: g.name for g in groups}
        self._groups_cache[panel.id] = (time.monotonic(), mapping)
        return mapping

    def clear_groups_cache(self, panel_id: int | None = None) -> None:
        """Clear the groups cache for one or all panels."""
        if panel_id is not None:
            self._groups_cache.pop(panel_id, None)
        else:
            self._groups_cache.clear()
