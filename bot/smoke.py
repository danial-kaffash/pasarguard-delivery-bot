"""Pre-flight checker — run once after filling .env, before going live:

    python -m bot.smoke

Verifies (without starting the bot or touching Telegram):
  1. the panel is reachable (with an SSL self-signed-cert hint on failure),
  2. panel admin credentials work,
  3. which groups exist on the panel,
  4. which of your curated offer groups are valid / stale.
"""

from __future__ import annotations

import asyncio

from panel.client import PasarGuardApiClient
from panel.exceptions import PanelAuthError, PanelError, PanelTransportError
from storage import db as store

from .config import get_settings
from .logging_setup import setup_logging


async def run() -> int:
    settings = get_settings()
    setup_logging("WARNING")
    print(f"PasarGuard greet-bot smoke check\npanel: {settings.panel_base_url}\n")

    async with PasarGuardApiClient(
        settings.panel_base_url,
        settings.panel_admin_username,
        settings.panel_admin_password,
        verify_ssl=settings.panel_verify_ssl,
        timeout=settings.panel_timeout_seconds,
    ) as panel:
        try:
            healthy = await panel.healthcheck()
        except PanelTransportError as exc:
            print(f"❌ Cannot reach the panel: {exc}")
            print(
                "   Hints: check PANEL_BASE_URL (scheme/port), and set "
                "PANEL_VERIFY_SSL=false if the panel uses a self-signed certificate."
            )
            return 1
        mark = "✅" if healthy else "⚠️"
        status = "OK" if healthy else "unexpected response"
        print(f"{mark} Panel /health: {status}")

        try:
            await panel.authenticate()
        except PanelAuthError as exc:
            print(f"❌ Panel login failed: {exc}")
            print("   Check PANEL_ADMIN_USERNAME / PANEL_ADMIN_PASSWORD.")
            return 1
        print("✅ Panel login OK")

        try:
            groups = await panel.list_groups_simple()
        except PanelError as exc:
            print(f"❌ Could not list groups (missing groups:read permission?): {exc}")
            return 1
        print(f"✅ Panel groups ({len(groups)}):")
        for g in groups:
            print(f"   {g.id:>4} — {g.name}")
        panel_ids = {g.id for g in groups}

        db = await store.connect(settings.db_path)
        try:
            seeded = await store.seed_offer_groups_from_file(db, settings.offer_groups_file)
            if seeded:
                print(f"\nSeeded {seeded} offer group(s) from {settings.offer_groups_file}")
            offers = await store.list_offer_groups(db)
        finally:
            await db.close()

        if not offers:
            print("\n⚠️ Offer list is EMPTY — trials are paused until you run /setoffer.")
        else:
            print(f"\nOffer list ({len(offers)}):")
            for o in offers:
                flag = "✅" if o.id in panel_ids else "⚠️ (id missing from panel!)"
                print(f"   {o.label} → id={o.id} {flag}")

    print("\nSmoke check complete.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
