"""First-run migration: seed the multi-tenant DB from legacy .env values.

Called once at startup when the DB has no panels or channels.
Reads the old single-tenant settings from .env and creates:
  - One panel entry (from PANEL_* vars)
  - One channel entry (from CHANNEL_ID + trial/promo vars)
  - Migrated offer_groups → channel_offer_groups
  - Migrated promo_state → per-channel
  - Migrated settings (paused, promo_text, etc.) → channel-scoped
  - OWNER_TG_IDS → superadmin users

After migration, the old .env values for these settings are no longer used
at runtime — everything comes from the DB.
"""

from __future__ import annotations

import logging

import aiosqlite

from storage import db as store

logger = logging.getLogger(__name__)


async def migrate_from_env(db: aiosqlite.Connection, settings) -> bool:
    """Seed the multi-tenant DB from .env if it's a fresh install.

    Returns True if migration was performed, False if already migrated.
    """
    panels = await store.list_panels(db, active_only=False)
    if panels:
        return False  # already have panels — no migration needed

    if not settings.panel_base_url or not settings.channel_id:
        logger.info("No panels/channels in DB and PANEL_BASE_URL or CHANNEL_ID not set — skipping migration.")
        return False

    logger.info("First run detected — migrating from .env to DB...")
    _migrated_items = []

    # 1. Create a panel from .env.
    panel = await store.create_panel(
        db,
        name="Default",
        base_url=settings.panel_base_url,
        admin_username=settings.panel_admin_username,
        admin_password=settings.panel_admin_password,
        verify_ssl=settings.panel_verify_ssl,
        timeout_seconds=settings.panel_timeout_seconds,
        protocols=settings.trial_protocols,
        auto_delete_days=settings.auto_delete_days,
    )
    _migrated_items.append(f"panel #{panel.id} ({panel.name})")

    # 2. Create a channel from .env.
    channel = await store.create_channel(
        db,
        tg_channel_id=settings.channel_id,
        title="",
        trial_data_limit_gb=settings.trial_data_limit_gb,
        trial_days=settings.trial_days,
        on_hold_grace_days=settings.on_hold_grace_days,
        allow_regrant_after_days=settings.allow_regrant_after_days,
        trial_max_member_age_days=settings.trial_max_member_age_days,
        join_approval_delay_seconds=settings.join_approval_delay_seconds,
        promo_interval_hours=settings.promo_interval_hours,
        promo_pin=settings.promo_pin,
        promo_silent=settings.promo_silent,
    )
    _migrated_items.append(f"channel #{channel.id} ({settings.channel_id})")

    # 3. Migrate legacy offer_groups → channel_offer_groups.
    legacy_offers = await store.list_offer_groups(db)
    for og in legacy_offers:
        await store.upsert_channel_offer_group(
            db, channel_id=channel.id, panel_id=panel.id,
            group_id=og.id, label=og.label,
        )
    if legacy_offers:
        _migrated_items.append(f"{len(legacy_offers)} offer group(s)")

    # 4. Migrate legacy promo_state → per-channel.
    legacy_state = await store.get_promo_state(db)
    if legacy_state:
        from bot.promo import set_channel_promo_state
        await set_channel_promo_state(
            db, channel.id, legacy_state.message_id, legacy_state.next_run_at,
        )
        _migrated_items.append("promo state")

    # 5. Migrate legacy global settings → channel-scoped.
    _settings_map = {
        "promo_text": f"channel:{channel.id}:promo_text",
        "promo_interval_hours": f"channel:{channel.id}:promo_interval_hours",
        "paused": f"channel:{channel.id}:paused",
        "joins_paused": f"channel:{channel.id}:joins_paused",
        "trial_max_member_age_days": f"channel:{channel.id}:trial_max_member_age_days",
        "join_approval_delay_seconds": f"channel:{channel.id}:join_approval_delay_seconds",
    }
    migrated_settings = 0
    for old_key, new_key in _settings_map.items():
        value = await store.get_setting(db, old_key)
        if value is not None:
            await store.set_setting(db, new_key, value)
            migrated_settings += 1
    if migrated_settings:
        _migrated_items.append(f"{migrated_settings} setting(s)")

    # 6. Seed OWNER_TG_IDS as superadmin users.
    for uid in settings.owner_tg_ids:
        await store.upsert_user(db, tg_user_id=uid, role="superadmin")
    if settings.owner_tg_ids:
        _migrated_items.append(f"{len(settings.owner_tg_ids)} superadmin(s)")

    logger.info(
        "Migration complete: %s. The bot is now multi-tenant.",
        ", ".join(_migrated_items),
    )
    return True
