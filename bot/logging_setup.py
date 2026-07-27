"""Logging configuration."""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, at startup."""
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    # Tame noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
