"""Entrypoint.

M1 stage: load & validate configuration, set up logging, report status.
The aiogram Dispatcher and handler wiring arrive in M3 (promo scheduler)
and M4 (trial flow) — see PLAN.md §10.
"""

from __future__ import annotations

import logging

from .config import get_settings
from .logging_setup import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("Configuration loaded.")
    logger.info(
        "Panel: %s (admin=%s, verify_ssl=%s)",
        settings.panel_base_url,
        settings.panel_admin_username,
        settings.panel_verify_ssl,
    )
    logger.info(
        "Trial: %.1f GB (%d bytes) on-hold — %d-day usage window, %d-day grace, protocols=%s",
        settings.trial_data_limit_gb,
        settings.trial_data_limit_bytes,
        settings.trial_days,
        settings.on_hold_grace_days,
        ",".join(settings.trial_protocol_list),
    )
    logger.info(
        "Promo: every %.1f h into channel %s (pin=%s, silent=%s)",
        settings.promo_interval_hours,
        settings.channel_id,
        settings.promo_pin,
        settings.promo_silent,
    )
    logger.info("M1 skeleton OK — bot handlers arrive in M3/M4.")


if __name__ == "__main__":
    main()
