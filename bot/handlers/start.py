"""Placeholder /start handler — the full trial flow (group selection → 5 GB
account → subscription URL) replaces this in M4."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router(name="start")


@router.message(CommandStart())
async def start_placeholder(message: Message) -> None:
    await message.answer(
        "سلام! 👋\n"
        "به‌زودی از همین‌جا می‌تونی تست ۵ گیگابایتی رایگانت رو دریافت کنی.\n"
        "🚧 این بخش در حال تکمیله — منتظرت می‌مونیم!"
    )
