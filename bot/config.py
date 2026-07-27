"""Application settings, loaded from environment variables / `.env`.

Every knob from PLAN.md 7 lives here. Values are validated at startup;
the process fails fast with a clear message if something is missing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GB = 1024**3  # the panel measures data limits in bytes


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: str = ""  # required to actually run the bot (M3+)
    channel_id: int = 0  # channel where the pinned promo post is published
    promo_interval_hours: float = 6.0  # how often to (re)post; runtime: /setinterval
    promo_pin: bool = True
    promo_silent: bool = True  # pin/post without notifying subscribers

    # --- PasarGuard panel ---------------------------------------------------
    panel_base_url: str
    panel_admin_username: str
    panel_admin_password: str
    panel_verify_ssl: bool = True  # set False for self-signed certs
    panel_timeout_seconds: float = 15.0

    # --- Trial (on-hold mode) -----------------------------------------------
    trial_data_limit_gb: float = 5.0
    trial_days: int = 3  # usage window after first connection
    on_hold_grace_days: int = 7  # must connect within this many days
    trial_protocols: str = "vless"  # comma-separated: vless,trojan,...
    offer_groups_file: Path = Path("data/offer_groups.json")
    auto_delete_days: int = 11  # grace + usage + margin; panel-side cleanup
    allow_regrant_after_days: int = 30  # cooldown before a user may re-claim

    # --- Owner / misc --------------------------------------------------------
    owner_tg_ids: list[int] = Field(default_factory=list)
    default_lang: str = "fa"
    db_path: Path = Path("data/bot.db")
    log_level: str = "INFO"
    rate_limit_per_minute: int = 30  # per-user flood protection
    trial_max_member_age_days: float = 0  # 0 = off; else only members newer than N days

    @field_validator("owner_tg_ids", mode="before")
    @classmethod
    def _split_csv_ids(cls, v):
        """Accept OWNER_TG_IDS as a bare id (95272833), a CSV string
        ("111,222"), or a JSON list ([111, 222]) — env sources may deliver
        any of the three depending on JSON-parseability."""
        if v is None or v == "":
            return []
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(part) for part in v.split(",") if part.strip()]
        return v

    @computed_field
    @property
    def trial_data_limit_bytes(self) -> int:
        """The panel's `data_limit` field expects bytes."""
        return int(self.trial_data_limit_gb * GB)

    @property
    def trial_protocol_list(self) -> list[str]:
        return [p.strip().lower() for p in self.trial_protocols.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton (clear cache in tests)."""
    return Settings()
