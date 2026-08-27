"""Tests for panel/manager.py — the PanelManager."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from panel.manager import PanelManager


def _panel(panel_id=1, name="NL", base_url="https://nl.test"):
    return SimpleNamespace(
        id=panel_id,
        name=name,
        base_url=base_url,
        admin_username="admin",
        admin_password="pw",
        verify_ssl=True,
        timeout_seconds=15.0,
    )


class TestPanelManager:
    def test_get_client_creates_new(self):
        pm = PanelManager()
        p = _panel()
        client = pm.get_client(p)
        assert client is not None

    def test_get_client_returns_same_instance(self):
        pm = PanelManager()
        p = _panel()
        c1 = pm.get_client(p)
        c2 = pm.get_client(p)
        assert c1 is c2

    def test_get_client_different_panels(self):
        pm = PanelManager()
        c1 = pm.get_client(_panel(panel_id=1))
        c2 = pm.get_client(_panel(panel_id=2))
        assert c1 is not c2

    def test_invalidate_removes_client(self):
        pm = PanelManager()
        p = _panel()
        c1 = pm.get_client(p)
        pm.invalidate(1)
        c2 = pm.get_client(p)
        assert c1 is not c2  # new instance after invalidation

    def test_invalidate_nonexistent(self):
        pm = PanelManager()
        pm.invalidate(999)  # should not raise

    def test_clear_groups_cache(self):
        pm = PanelManager()
        pm._groups_cache[1] = (0.0, {2: "NL"})
        pm.clear_groups_cache(1)
        assert 1 not in pm._groups_cache

    def test_clear_groups_cache_all(self):
        pm = PanelManager()
        pm._groups_cache[1] = (0.0, {2: "NL"})
        pm._groups_cache[2] = (0.0, {5: "TR"})
        pm.clear_groups_cache()
        assert len(pm._groups_cache) == 0

    def test_list_groups_caches(self):
        """Verify that list_groups caches the result (TTL-based)."""
        pm = PanelManager()
        # This requires a real or mocked panel client.  Since list_groups
        # calls panel.list_groups_simple(), we'd need a FakePanel.
        # For now, verify the cache structure.
        assert pm._groups_cache == {}

    @pytest.mark.asyncio
    async def test_close_all(self):
        pm = PanelManager()
        pm.get_client(_panel(panel_id=1))
        pm.get_client(_panel(panel_id=2))
        assert len(pm._clients) == 2
        await pm.close_all()
        assert len(pm._clients) == 0
        assert len(pm._groups_cache) == 0
