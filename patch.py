# patch_pause.py — adds /pause + /resume master switch
import pathlib


def detect_nl(raw: str) -> str:
    return "\r\n" if "\r\n" in raw else "\n"


def patch(path, old, new):
    p = pathlib.Path(path)
    raw = p.read_bytes().decode("utf-8")
    nl = detect_nl(raw)
    src = raw.replace("\r\n", "\n")
    n = src.count(old)
    assert n == 1, f"ANCHOR NOT FOUND in {path} (found {n}) - file differs or already patched; aborting"
    p.write_bytes(src.replace(old, new, 1).replace("\n", nl).encode("utf-8"))
    print(f"patched {path}")


# 0) new file: bot/pause.py
p = pathlib.Path("bot/pause.py")
assert not p.exists(), "bot/pause.py already exists - aborting"
nl = detect_nl(pathlib.Path("bot/promo.py").read_bytes().decode("utf-8"))
pause_src = '''"""Master pause switch - stops promo posting and trial delivery until resumed."""

from __future__ import annotations

import aiosqlite

from storage import db as store

PAUSED_KEY = "paused"

_TRUE_VALUES = {"1", "true", "yes", "on"}


async def is_paused(db: aiosqlite.Connection) -> bool:
    raw = await store.get_setting(db, PAUSED_KEY, "false")
    return (raw or "").strip().lower() in _TRUE_VALUES


async def set_paused(db: aiosqlite.Connection, value: bool) -> None:
    await store.set_setting(db, PAUSED_KEY, "true" if value else "false")
'''
p.write_bytes(pause_src.replace("\n", nl).encode("utf-8"))
print("created bot/pause.py")

# 1) promo scheduler: skip posts while paused
patch("bot/promo.py",
r'''from storage import db as store

logger = logging.getLogger(__name__)''',
r'''from storage import db as store

from .pause import is_paused

logger = logging.getLogger(__name__)''')

patch("bot/promo.py",
"RETRY_BACKOFF_SECONDS = 60.0",
"RETRY_BACKOFF_SECONDS = 60.0\nPAUSE_POLL_SECONDS = 60.0  # re-check pause flag interval")

patch("bot/promo.py",
r'''        logger.info("Next promo post in %.0f s", wait)
        await asyncio.sleep(wait)
        try:''',
r'''        logger.info("Next promo post in %.0f s", wait)
        await asyncio.sleep(wait)
        if await is_paused(db):
            logger.info(
                "Bot paused - skipping promo post; re-checking in %.0f s.", PAUSE_POLL_SECONDS
            )
            await asyncio.sleep(PAUSE_POLL_SECONDS)
            continue  # next_run stays in the past -> posts promptly after /resume
        try:''')

# 2) trial flow: gate /start and confirm
patch("bot/handlers/trial.py",
r'''from .. import texts
from ..keyboards import GroupCB, build_selection_keyboard''',
r'''from .. import texts
from ..keyboards import GroupCB, build_selection_keyboard
from ..pause import is_paused''')

patch("bot/handlers/trial.py",
r'''    if message.chat.type != "private":
        await message.answer(texts.PRIVATE_ONLY)
        return

    user = message.from_user''',
r'''    if message.chat.type != "private":
        await message.answer(texts.PRIVATE_ONLY)
        return

    if await is_paused(db):
        await message.answer(texts.PAUSED)
        return

    user = message.from_user''')

patch("bot/handlers/trial.py",
r'''    if not selected:
        await callback.answer(texts.SELECT_HINT, show_alert=True)
        return''',
r'''    if not selected:
        await callback.answer(texts.SELECT_HINT, show_alert=True)
        return

    if await is_paused(db):
        await state.clear()
        await callback.message.edit_text(texts.PAUSED)
        await callback.answer()
        return''')

# 3) texts: append PAUSED message (no Persian anchor needed)
p = pathlib.Path("bot/texts.py")
raw = p.read_bytes().decode("utf-8")
if "PAUSED =" in raw:
    print("bot/texts.py already has PAUSED - skipped")
else:
    nl = detect_nl(raw)
    add = 'PAUSED = "⏸ دریافت تست رایگان موقتاً متوقف شده.\\nلطفاً بعداً دوباره سر بزن. 🙏"\n'
    out = raw.rstrip("\r\n") + nl + nl + add.replace("\n", nl)
    p.write_bytes(out.encode("utf-8"))
    print("patched bot/texts.py")

# 4) admin: /pause and /resume commands
patch("bot/handlers/admin.py",
r'''from storage import db as store

from ..promo import (''',
r'''from storage import db as store

from ..pause import set_paused
from ..promo import (''')

patch("bot/handlers/admin.py",
'@router.message(IsOwner(), Command("reset"))',
r'''@router.message(IsOwner(), Command("pause"))
async def cmd_pause(message: Message, db: aiosqlite.Connection) -> None:
    await set_paused(db, True)
    await message.answer(
        "⏸ ربات متوقف شد: پست تبلیغاتی و تحویل تست ارسال نمی‌شه.\nبرای شروع دوباره: /resume"
    )


@router.message(IsOwner(), Command("resume"))
async def cmd_resume(message: Message, db: aiosqlite.Connection) -> None:
    await set_paused(db, False)
    await message.answer(
        "▶️ ربات دوباره فعال شد.\nاگه زمان پست تبلیغاتی رسیده باشه، تا یک دقیقهٔ دیگه ارسال می‌شه."
    )


@router.message(IsOwner(), Command("reset"))''')

print("\nDone. Review with:  git diff")