"""Tests for bot.config.Settings."""

from __future__ import annotations

import pytest

from bot.config import GB, Settings


def _base_kwargs(**overrides):
    kwargs = {
        "panel_base_url": "https://panel.test:8000",
        "panel_admin_username": "greet-bot",
        "panel_admin_password": "secret",
    }
    kwargs.update(overrides)
    return kwargs


def test_defaults_and_bytes_conversion():
    s = Settings(**_base_kwargs())
    assert s.trial_data_limit_gb == 5.0
    assert s.trial_data_limit_bytes == 5 * GB
    assert s.trial_days == 3
    assert s.on_hold_grace_days == 7
    assert s.trial_protocol_list == ["vless"]
    assert s.promo_interval_hours == 6.0
    assert s.owner_tg_ids == []


def test_custom_gb_and_protocols():
    s = Settings(**_base_kwargs(trial_data_limit_gb=2.5, trial_protocols="vless, trojan"))
    assert s.trial_data_limit_bytes == int(2.5 * GB)
    assert s.trial_protocol_list == ["vless", "trojan"]


def test_owner_ids_csv_parsing():
    s = Settings(**_base_kwargs(owner_tg_ids="111, 222 ,333"))
    assert s.owner_tg_ids == [111, 222, 333]


def test_owner_ids_list_passthrough():
    s = Settings(**_base_kwargs(owner_tg_ids=[42]))
    assert s.owner_tg_ids == [42]


def test_env_var_override(monkeypatch):
    monkeypatch.setenv("PANEL_BASE_URL", "https://env.panel:8000")
    monkeypatch.setenv("PANEL_ADMIN_USERNAME", "envuser")
    monkeypatch.setenv("PANEL_ADMIN_PASSWORD", "envpass")
    monkeypatch.setenv("PROMO_INTERVAL_HOURS", "12")
    s = Settings()
    assert s.panel_base_url == "https://env.panel:8000"
    assert s.promo_interval_hours == 12.0


def test_missing_panel_settings_defaults_to_empty(monkeypatch):
    # Panel settings are now optional (managed through the bot).
    for var in ("PANEL_BASE_URL", "PANEL_ADMIN_USERNAME", "PANEL_ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.panel_base_url == ""
    assert s.panel_admin_username == ""
    assert s.panel_admin_password == ""
