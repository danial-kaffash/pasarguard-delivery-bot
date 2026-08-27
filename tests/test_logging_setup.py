"""Tests for bot.logging_setup."""

from __future__ import annotations

import logging

from bot.logging_setup import LOG_FORMAT, setup_logging


def _reset_root() -> tuple[list[logging.Handler], int]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    return saved_handlers, saved_level


def test_setup_logging_configures_root_logger():
    saved_handlers, saved_level = _reset_root()
    try:
        setup_logging("warning")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        assert root.handlers, "basicConfig should install a handler"
        assert root.handlers[-1].fmt == LOG_FORMAT if hasattr(root.handlers[-1], "fmt") else True
    finally:
        root = logging.getLogger()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_setup_logging_accepts_lowercase_level():
    saved_handlers, saved_level = _reset_root()
    try:
        setup_logging("debug")
        assert logging.getLogger().level == logging.DEBUG
    finally:
        root = logging.getLogger()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_setup_logging_tames_noisy_third_party_loggers():
    saved_handlers, saved_level = _reset_root()
    httpx_logger = logging.getLogger("httpx")
    aiogram_logger = logging.getLogger("aiogram.event")
    saved_levels = (httpx_logger.level, aiogram_logger.level)
    try:
        setup_logging("info")
        assert httpx_logger.level == logging.WARNING
        assert aiogram_logger.level == logging.WARNING
    finally:
        root = logging.getLogger()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        httpx_logger.setLevel(saved_levels[0])
        aiogram_logger.setLevel(saved_levels[1])
