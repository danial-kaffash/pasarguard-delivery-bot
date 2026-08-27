"""Tests for services/channel_settings.py — the ChannelSettings adapter."""

from __future__ import annotations

from services.channel_settings import ChannelSettings
from storage.db import Channel, Panel


def _panel(**overrides):
    base = dict(
        id=1,
        name="NL",
        base_url="https://nl.test",
        admin_username="admin",
        admin_password="pw",
        verify_ssl=True,
        timeout_seconds=15.0,
        protocols="vless,trojan",
        auto_delete_days=11,
        active=True,
    )
    base.update(overrides)
    return Panel(**base)


def _channel(**overrides):
    base = dict(
        id=1,
        tg_channel_id=-100123,
        title="Test",
        trial_data_limit_gb=10.0,
        trial_days=7,
        on_hold_grace_days=14,
        allow_regrant_after_days=60,
        trial_max_member_age_days=3.0,
        join_approval_delay_seconds=30,
        promo_interval_hours=12.0,
        promo_pin=False,
        promo_silent=False,
        active=True,
    )
    base.update(overrides)
    return Channel(**base)


def test_trial_data_limit_gb():
    cs = ChannelSettings(_channel(trial_data_limit_gb=10.0), _panel())
    assert cs.trial_data_limit_gb == 10.0


def test_trial_data_limit_bytes():
    cs = ChannelSettings(_channel(trial_data_limit_gb=5.0), _panel())
    assert cs.trial_data_limit_bytes == 5 * 1024**3


def test_trial_days():
    cs = ChannelSettings(_channel(trial_days=7), _panel())
    assert cs.trial_days == 7


def test_on_hold_grace_days():
    cs = ChannelSettings(_channel(on_hold_grace_days=14), _panel())
    assert cs.on_hold_grace_days == 14


def test_allow_regrant_after_days():
    cs = ChannelSettings(_channel(allow_regrant_after_days=60), _panel())
    assert cs.allow_regrant_after_days == 60


def test_trial_max_member_age_days():
    cs = ChannelSettings(_channel(trial_max_member_age_days=3.0), _panel())
    assert cs.trial_max_member_age_days == 3.0


def test_join_approval_delay_seconds():
    cs = ChannelSettings(_channel(join_approval_delay_seconds=30), _panel())
    assert cs.join_approval_delay_seconds == 30


def test_auto_delete_days_from_panel():
    cs = ChannelSettings(_channel(), _panel(auto_delete_days=14))
    assert cs.auto_delete_days == 14


def test_trial_protocol_list():
    cs = ChannelSettings(_channel(), _panel(protocols="vless,trojan"))
    assert cs.trial_protocol_list == ["vless", "trojan"]


def test_trial_protocol_list_single():
    cs = ChannelSettings(_channel(), _panel(protocols="vless"))
    assert cs.trial_protocol_list == ["vless"]


def test_channel_id():
    cs = ChannelSettings(_channel(tg_channel_id=-100999), _panel())
    assert cs.channel_id == -100999


def test_channel_property():
    ch = _channel()
    cs = ChannelSettings(ch, _panel())
    assert cs.channel is ch


def test_panel_property():
    p = _panel()
    cs = ChannelSettings(_channel(), p)
    assert cs.panel is p
