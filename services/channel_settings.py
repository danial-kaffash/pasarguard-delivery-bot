"""Adapter that makes a Channel+Panel look like the Settings object
that existing service functions expect.

This lets us reuse ``build_trial_user``, ``check_eligibility``, and
``create_trial`` without rewriting their internals — they access settings
attributes via duck typing, so we just need to expose the same names.
"""

from __future__ import annotations

from storage.db import Channel, Panel


class ChannelSettings:
    """Wraps a Channel and its Panel so trial-service functions work unchanged.

    Usage::

        cs = ChannelSettings(channel, panel)
        user = build_trial_user(settings=cs, username=..., ...)
    """

    def __init__(self, channel: Channel, panel: Panel) -> None:
        self._channel = channel
        self._panel = panel

    @property
    def channel(self) -> Channel:
        return self._channel

    @property
    def panel(self) -> Panel:
        return self._panel

    # ── attributes matching Settings interface ──────────────────────────────

    @property
    def trial_data_limit_gb(self) -> float:
        return self._channel.trial_data_limit_gb

    @property
    def trial_data_limit_bytes(self) -> int:
        return int(self._channel.trial_data_limit_gb * 1024**3)

    @property
    def trial_days(self) -> int:
        return self._channel.trial_days

    @property
    def on_hold_grace_days(self) -> int:
        return self._channel.on_hold_grace_days

    @property
    def allow_regrant_after_days(self) -> int:
        return self._channel.allow_regrant_after_days

    @property
    def trial_max_member_age_days(self) -> float:
        return self._channel.trial_max_member_age_days

    @property
    def join_approval_delay_seconds(self) -> int:
        return self._channel.join_approval_delay_seconds

    @property
    def auto_delete_days(self) -> int:
        return self._panel.auto_delete_days

    @property
    def trial_protocol_list(self) -> list[str]:
        return [p.strip().lower() for p in self._panel.protocols.split(",") if p.strip()]

    # ── channel-level identifiers ───────────────────────────────────────────

    @property
    def channel_id(self) -> int:
        """The Telegram channel id (e.g. -1001234567890)."""
        return self._channel.tg_channel_id
