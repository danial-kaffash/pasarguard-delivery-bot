"""Tests for services/posts.py — keyboard building, entities, scheduling, sending."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MessageEntity

from services import posts as svc
from storage import db as store

BUTTON_URL = {
    "label": "دریافت تست",
    "action": {"type": "url", "url": "https://t.me/bot?start=join"},
    "style": "success",
    "row": 0,
}
BUTTON_NOOP = {"label": "فقط متن", "action": {"type": "disabled"}, "row": 1}
BUTTON_COPY = {
    "label": "کپی کن",
    "action": {"type": "copy", "text": "SECRET"},
    "style": "danger",
    "row": 1,
}
BUTTON_ICON = {
    "label": "🎉 Party",
    "label_clean": "Party",
    "label_fallback": "🎉 Party",
    "icon": "5312345678",
    "row": 2,
}


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _channel(db):
    return await store.create_channel(db, tg_channel_id=-100123, title="Test")


def _make_post(db_id: int, **kw) -> store.ChannelPost:
    base = dict(
        id=1,
        channel_id=db_id,
        group_id=None,
        created_by=1,
        created_at="",
        updated_at="",
        text="hello",
        entities_json=None,
        media_type=None,
        media_file_id=None,
        media_json=None,
        buttons_json="[]",
        delete_previous=False,
        pin=False,
        silent=False,
        link_preview=True,
        ephemeral_hours=None,
        expires_at=None,
        status="sent",
        scheduled_at=None,
        recurrence="none",
        recur_at=None,
        last_sent_at=None,
        sent_at=None,
        tg_message_id=None,
        tg_message_ids_json=None,
        error=None,
    )
    base.update(kw)
    return store.ChannelPost(**base)


# ── UTF-16 span math ─────────────────────────────────────────────────────────


def test_utf16_span_bmp_only():
    text = "hello"
    assert text[slice(*svc._utf16_span(text, 1, 3))] == "ell"


def test_utf16_span_with_astral_emoji():
    # 🎉 is U+1F389 — 2 UTF-16 code units, 1 code point.
    text = "a🎉b"
    start, end = svc._utf16_span(text, 1, 2)
    assert text[start:end] == "🎉"
    start, end = svc._utf16_span(text, 0, 1)
    assert text[start:end] == "a"
    start, end = svc._utf16_span(text, 3, 1)
    assert text[start:end] == "b"


def test_extract_button_icon_finds_premium_emoji():
    # "🎉" is one code point / two UTF-16 units at offset 0.
    text = "🎉 بفرست"
    entities = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "531"}]
    result = svc.extract_button_icon(text, entities)
    assert result == ("531", "بفرست", "🎉")


def test_extract_button_icon_none_for_plain_text():
    assert svc.extract_button_icon("plain", None) is None
    assert svc.extract_button_icon("bold", [{"type": "bold", "offset": 0, "length": 4}]) is None


# ── keyboard building ────────────────────────────────────────────────────────


def test_build_keyboard_none_without_buttons():
    assert svc.build_keyboard([]) is None


def test_build_keyboard_styles_actions_rows():
    kb = svc.build_keyboard([BUTTON_URL, BUTTON_NOOP, BUTTON_COPY])
    assert len(kb.inline_keyboard) == 2  # row 0 + row 1 (two buttons)
    (url_btn,) = kb.inline_keyboard[0]
    assert url_btn.url == "https://t.me/bot?start=join"
    assert url_btn.style == "success"
    noop_btn, copy_btn = kb.inline_keyboard[1]
    assert noop_btn.disabled is not None
    assert copy_btn.copy_text is not None and copy_btn.copy_text.text == "SECRET"
    assert copy_btn.style == "danger"


def test_build_keyboard_icon_and_fallback():
    kb = svc.build_keyboard([BUTTON_ICON], with_icons=True)
    (btn,) = kb.inline_keyboard[0]
    assert btn.icon_custom_emoji_id == "5312345678"
    assert btn.text == "Party"  # emoji char stripped when the icon renders it

    kb_fb = svc.build_keyboard([BUTTON_ICON], with_icons=False)
    (btn_fb,) = kb_fb.inline_keyboard[0]
    assert btn_fb.icon_custom_emoji_id is None
    assert btn_fb.text == "🎉 Party"  # plain emoji char back


def test_build_keyboard_default_row_is_one_per_row():
    kb = svc.build_keyboard([{"label": "a"}, {"label": "b"}, {"label": "c"}])
    assert len(kb.inline_keyboard) == 3


# ── entities round-trip ──────────────────────────────────────────────────────


def test_entities_roundtrip_preserves_custom_emoji():
    ents = [
        MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="531"),
        MessageEntity(type="bold", offset=3, length=2),
    ]
    raw = svc.entities_to_json(ents)
    restored = svc.entities_from_json(raw)
    assert restored[0].type == "custom_emoji" and restored[0].custom_emoji_id == "531"
    assert restored[1].type == "bold"
    assert svc.strip_custom_emoji_entities(restored)[0].type == "bold"
    assert svc.strip_custom_emoji_entities(None) is None


def test_entities_from_bad_json_is_none():
    assert svc.entities_from_json(None) is None
    assert svc.entities_from_json("not json") is None


# ── schedule parsing / recurrence ────────────────────────────────────────────


def test_parse_schedule_full_datetime_tehran_to_utc():
    now = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)  # 11:30 Tehran
    dt = svc.parse_schedule_input("2026-08-28 14:30", now)
    assert dt == datetime(2026, 8, 28, 11, 0, tzinfo=UTC)  # 14:30 +03:30


def test_parse_schedule_time_only_future_today():
    now = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)  # 11:30 Tehran
    dt = svc.parse_schedule_input("12:00", now)  # later today, Tehran
    assert dt == datetime(2026, 8, 27, 8, 30, tzinfo=UTC)


def test_parse_schedule_time_only_rolls_to_tomorrow():
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)  # 15:30 Tehran
    dt = svc.parse_schedule_input("10:00", now)  # already past today
    assert dt == datetime(2026, 8, 28, 6, 30, tzinfo=UTC)


def test_parse_schedule_rejects_garbage():
    with pytest.raises(ValueError):
        svc.parse_schedule_input("sometime", datetime.now(UTC))


def test_next_occurrence_daily():
    post = _make_post(
        1, recurrence="daily", recur_at="14:00", scheduled_at="2026-08-27T10:30:00+00:00"
    )
    now = datetime(2026, 8, 27, 10, 30, tzinfo=UTC)  # exactly the scheduled time
    nxt = svc.next_occurrence(post, now)
    assert nxt == datetime(2026, 8, 28, 10, 30, tzinfo=UTC)  # next day 14:00 Tehran


def test_next_occurrence_weekly_and_midday_alignment():
    post = _make_post(
        1, recurrence="weekly", recur_at="09:00", scheduled_at="2026-08-27T05:30:00+00:00"
    )  # Thu 09:00 Tehran
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)  # later that day
    nxt = svc.next_occurrence(post, now)
    # After a send at 12:00 UTC, the next aligned slot is next Thursday 09:00 Tehran,
    # which is strictly after the send time.
    assert nxt > now
    assert nxt == datetime(2026, 9, 3, 5, 30, tzinfo=UTC)


def test_parse_hhmm_validates():
    assert svc.parse_hhmm("14:05") == (14, 5)
    for bad in ("25:00", "12:60", "14", None, "a:b"):
        with pytest.raises(ValueError):
            svc.parse_hhmm(bad)


# ── send kwargs ──────────────────────────────────────────────────────────────


def test_build_send_kwargs_text_post():
    post = _make_post(1, text="hi")
    kw = svc.build_send_kwargs(post, entities=None, keyboard=None, chat_id=-100)
    assert kw["text"] == "hi"
    assert kw["chat_id"] == -100
    assert kw["link_preview_options"].is_disabled is False
    assert "caption" not in kw


def test_build_send_kwargs_media_post_with_caption():
    post = _make_post(1, text="cap", media_type="photo", media_file_id="F1", link_preview=False)
    ents = [MessageEntity(type="bold", offset=0, length=3)]
    kw = svc.build_send_kwargs(post, entities=ents, keyboard=None)
    assert kw["caption"] == "cap"
    assert kw["caption_entities"] == ents
    # Media methods have no link_preview_options parameter — must be absent.
    assert "link_preview_options" not in kw


# ── sending (fake bot) ───────────────────────────────────────────────────────


class FakeSendBot:
    def __init__(self, *, fail_on_custom_emoji: bool = False):
        self.sent: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.pinned: list[dict] = []
        self.fail_on_custom_emoji = fail_on_custom_emoji
        self._next_id = 100

    def _has_premium(self, kwargs: dict) -> bool:
        for e in kwargs.get("entities") or []:
            if e.type == "custom_emoji":
                return True
        for item in kwargs.get("media") or []:
            for e in getattr(item, "caption_entities", None) or []:
                if e.type == "custom_emoji":
                    return True
        kb = kwargs.get("reply_markup")
        if kb:
            return any(
                getattr(b, "icon_custom_emoji_id", None) for row in kb.inline_keyboard for b in row
            )
        return False

    async def send_message(self, chat_id, text=None, **kwargs):
        if self.fail_on_custom_emoji and self._has_premium(kwargs):
            raise TelegramBadRequest(
                method="sendMessage", message="Bad Request: CUSTOM_EMOJI_INVALID"
            )
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def send_media_group(self, chat_id, media=None, **kwargs):
        # Mirror the real signature: no reply_markup, no link_preview_options.
        for forbidden in ("reply_markup", "link_preview_options", "text"):
            assert forbidden not in kwargs, f"send_media_group got forbidden kwarg {forbidden}"
        if self.fail_on_custom_emoji and self._has_premium({"media": media}):
            raise TelegramBadRequest(
                method="sendMediaGroup", message="Bad Request: CUSTOM_EMOJI_INVALID"
            )
        ids = []
        for _ in media or []:
            self._next_id += 1
            ids.append(SimpleNamespace(message_id=self._next_id))
        self.sent.append({"chat_id": chat_id, "media": media, "media_group_ids": ids, **kwargs})
        return ids

    async def send_photo(self, chat_id, photo=None, caption=None, **kwargs):
        # Mirror the real Bot.send_photo signature: these kwargs are TypeErrors.
        for forbidden in ("link_preview_options", "text", "entities"):
            assert forbidden not in kwargs, f"send_photo got forbidden kwarg {forbidden}"
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "photo": photo, "caption": caption, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def send_video(self, chat_id, video=None, caption=None, **kwargs):
        for forbidden in ("link_preview_options", "text", "entities"):
            assert forbidden not in kwargs, f"send_video got forbidden kwarg {forbidden}"
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "video": video, "caption": caption, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def send_animation(self, chat_id, animation=None, caption=None, **kwargs):
        for forbidden in ("link_preview_options", "text", "entities"):
            assert forbidden not in kwargs, f"send_animation got forbidden kwarg {forbidden}"
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "animation": animation, "caption": caption, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append({"chat_id": chat_id, "message_id": message_id, **kwargs})


async def test_send_post_text_with_buttons(db):
    ch = await _channel(db)
    post = _make_post(ch.id, text="پست تست", buttons_json=json.dumps([BUTTON_URL]), pin=True)
    bot = FakeSendBot()
    result = await svc.send_post(bot, db, post, ch)
    assert result.message_id > 0
    assert result.used_fallback is False
    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == "پست تست"
    kb = bot.sent[0]["reply_markup"]
    assert kb.inline_keyboard[0][0].url == BUTTON_URL["action"]["url"]
    assert len(bot.pinned) == 1


async def test_send_post_deletes_previous(db):
    ch = await _channel(db)
    prev = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="sent",
        text="old",
        buttons_json="[]",
    )
    await store.update_channel_post(
        db, prev.id, tg_message_id=55, sent_at="2026-01-01T00:00:00+00:00"
    )
    post = _make_post(ch.id, id=2, text="new", delete_previous=True)
    bot = FakeSendBot()
    await svc.send_post(bot, db, post, ch)
    assert bot.deleted == [(-100123, 55)]


async def test_send_post_ephemeral_sets_expiry(db):
    ch = await _channel(db)
    post = _make_post(ch.id, ephemeral_hours=6)
    now = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    result = await svc.send_post(FakeSendBot(), db, post, ch, now=now)
    assert result.expires_at == (now + timedelta(hours=6)).isoformat()


async def test_send_post_premium_fallback(db):
    ch = await _channel(db)
    ents = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "531"}]
    post = _make_post(
        ch.id,
        text="🎉 سلام",
        entities_json=json.dumps(ents),
        buttons_json=json.dumps([BUTTON_ICON]),
    )
    bot = FakeSendBot(fail_on_custom_emoji=True)
    result = await svc.send_post(bot, db, post, ch, fallback_notify_chat_id=99)
    assert result.used_fallback is True
    # first attempt raised, then: stripped retry to the channel + warning DM to the admin
    assert len(bot.sent) == 2
    retry = bot.sent[0]
    assert retry["chat_id"] == -100123
    assert not retry.get("entities")  # custom-emoji entities stripped (and omitted when empty)
    kb = retry["reply_markup"]
    assert kb.inline_keyboard[0][0].icon_custom_emoji_id is None
    assert kb.inline_keyboard[0][0].text == "🎉 Party"
    assert bot.sent[1]["chat_id"] == 99  # the warning DM


async def test_send_post_media_uses_photo_method(db):
    ch = await _channel(db)
    post = _make_post(ch.id, text="cap", media_type="photo", media_file_id="PHOTO1")
    bot = FakeSendBot()
    await svc.send_post(bot, db, post, ch)
    assert bot.sent[0]["photo"] == "PHOTO1"
    assert bot.sent[0]["caption"] == "cap"


async def test_send_post_media_never_gets_link_preview_options(db):
    """Regression: send_photo/video/animation have no link_preview_options.

    Production crash (2026-08-27): previewing a media post raised
    ``Bot.send_photo() got an unexpected keyword argument
    'link_preview_options'``. FakeSendBot.send_* now assert the forbidden
    kwarg is absent, so a regression fails loudly here.
    """
    ch = await _channel(db)
    for media_type, key in (("photo", "photo"), ("video", "video"), ("animation", "animation")):
        post = _make_post(
            ch.id,
            text="cap",
            media_type=media_type,
            media_file_id="F1",
            link_preview=False,
        )
        bot = FakeSendBot()
        await svc.send_post(bot, db, post, ch)
        assert bot.sent[0][key] == "F1"
        assert bot.sent[0]["caption"] == "cap"


# ── dispatch_due_posts ───────────────────────────────────────────────────────


async def test_dispatch_sends_due_one_shot(db):
    ch = await _channel(db)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="scheduled",
        text="due",
        scheduled_at=past,
    )
    bot = FakeSendBot()
    sent = await svc.dispatch_due_posts(bot, db)
    assert sent == 1
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "sent"
    assert updated.tg_message_id is not None


async def test_dispatch_skips_future_scheduled(db):
    ch = await _channel(db)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="scheduled",
        text="later",
        scheduled_at=future,
    )
    assert await svc.dispatch_due_posts(FakeSendBot(), db) == 0


async def test_dispatch_recurring_updates_next_run(db):
    ch = await _channel(db)
    now = datetime.now(UTC)
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="recurring",
        text="rec",
        scheduled_at=now.isoformat(),
        recurrence="daily",
        recur_at="09:00",
    )
    bot = FakeSendBot()
    assert await svc.dispatch_due_posts(bot, db) == 1
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "recurring"
    assert updated.last_sent_at is not None
    assert svc.parse_dt(updated.scheduled_at) > now  # next occurrence scheduled


async def test_dispatch_expires_ephemeral_post(db):
    ch = await _channel(db)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="sent",
        text="gone",
    )
    await store.update_channel_post(db, post.id, tg_message_id=77, expires_at=past)
    bot = FakeSendBot()
    await svc.dispatch_due_posts(bot, db)
    assert bot.deleted == [(-100123, 77)]
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "expired"
    assert updated.tg_message_id is None and updated.expires_at is None


async def test_dispatch_failure_marks_post_failed(db):
    ch = await _channel(db)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    class ExplodingBot(FakeSendBot):
        async def send_message(self, chat_id, text=None, **kwargs):
            raise TelegramBadRequest(method="sendMessage", message="Bad Request: chat not found")

    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="scheduled",
        text="boom",
        scheduled_at=past,
    )
    await svc.dispatch_due_posts(ExplodingBot(), db)
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "failed"
    assert "chat not found" in updated.error


# ── edit_published_post ──────────────────────────────────────────────────────


async def test_edit_published_text_post(db):
    ch = await _channel(db)
    post = _make_post(
        ch.id, text="نسخهٔ جدید", tg_message_id=42, buttons_json=json.dumps([BUTTON_URL])
    )

    edits: list[dict] = []

    class EditBot:
        async def edit_message_text(self, **kwargs):
            # edit_message_text DOES accept link_preview_options
            edits.append(kwargs)

    assert await svc.edit_published_post(EditBot(), post, ch) is True
    assert edits[0]["text"] == "نسخهٔ جدید"
    assert edits[0]["message_id"] == 42
    assert edits[0]["reply_markup"].inline_keyboard[0][0].url == BUTTON_URL["action"]["url"]
    assert edits[0]["link_preview_options"].is_disabled is False


async def test_edit_published_media_post_edits_caption(db):
    ch = await _channel(db)
    post = _make_post(ch.id, text="کپشن", media_type="photo", tg_message_id=43)

    edits: list[dict] = []

    class EditBot:
        async def edit_message_caption(self, **kwargs):
            # Mirror the real signature: edit_message_caption has NO
            # link_preview_options parameter — passing it is a TypeError.
            assert "link_preview_options" not in kwargs, (
                "edit_message_caption got forbidden kwarg link_preview_options"
            )
            edits.append(kwargs)

    await svc.edit_published_post(EditBot(), post, ch)
    assert edits[0]["caption"] == "کپشن"


async def test_edit_without_message_id_is_rejected(db):
    ch = await _channel(db)
    post = _make_post(ch.id)
    assert await svc.edit_published_post(FakeSendBot(), post, ch) is False


# ── albums (media groups) ────────────────────────────────────────────────────


def _album_post(ch_id: int, **kw) -> store.ChannelPost:
    media = json.dumps(
        [
            {"type": "photo", "file_id": "A1"},
            {"type": "photo", "file_id": "A2"},
            {"type": "video", "file_id": "A3"},
        ]
    )
    return _make_post(ch_id, text="کپشن آلبوم", media_type="album", media_json=media, **kw)


def test_media_items_json_helpers():
    items = svc.media_items_from_json(
        json.dumps([{"type": "photo", "file_id": "A"}, {"type": "bogus", "file_id": "B"}])
    )
    assert items == [{"type": "photo", "file_id": "A"}]
    assert svc.media_items_from_json(None) == []
    assert svc.media_items_from_json("not json") == []
    assert svc.media_items_to_json([{"type": "video", "file_id": "V", "junk": 1}]) == (
        '[{"type": "video", "file_id": "V"}]'
    )
    assert svc.message_ids_from_json("[3, 4, 5]") == [3, 4, 5]
    assert svc.message_ids_from_json("x") == []


def test_is_album_and_post_message_ids():
    post = _album_post(1)
    assert svc.is_album(post) is True
    assert svc.is_album(_make_post(1, media_type="photo", media_file_id="P")) is False
    assert svc.post_message_ids(_make_post(1, tg_message_id=9)) == [9]
    assert svc.post_message_ids(
        _make_post(1, tg_message_id=9, tg_message_ids_json="[9, 10, 11]")
    ) == ([9, 10, 11])
    assert svc.post_message_ids(_make_post(1)) == []


async def test_send_post_album(db):
    ch = await _channel(db)
    post = _album_post(ch.id, pin=True)
    ents = [MessageEntity(type="bold", offset=0, length=5)]
    post.entities_json = svc.entities_to_json(ents)
    bot = FakeSendBot()
    result = await svc.send_post(bot, db, post, ch)
    assert len(bot.sent) == 1
    sent = bot.sent[0]
    media = sent["media"]
    assert len(media) == 3
    assert sent["disable_notification"] is False
    # caption + entities ride on the FIRST item only
    assert media[0].caption == "کپشن آلبوم"
    assert media[0].caption_entities == ents
    assert media[1].caption is None and media[2].caption is None
    # message ids from the group; the first one is pinned
    assert len(result.message_ids) == 3
    assert result.message_id == result.message_ids[0]
    assert bot.pinned[0]["message_id"] == result.message_id


async def test_send_post_album_premium_fallback(db):
    ch = await _channel(db)
    ents = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "531"}]
    post = _album_post(ch.id)
    post.text = "🎉 سلام"
    post.entities_json = json.dumps(ents)
    bot = FakeSendBot(fail_on_custom_emoji=True)
    result = await svc.send_post(bot, db, post, ch, fallback_notify_chat_id=99)
    assert result.used_fallback is True
    assert len(bot.sent) == 2  # retry + warning DM
    retry_media = bot.sent[0]["media"]
    # custom-emoji entities stripped (and omitted entirely once empty)
    assert not retry_media[0].caption_entities
    assert bot.sent[1]["chat_id"] == 99


async def test_send_post_delete_previous_album(db):
    ch = await _channel(db)
    prev = await store.create_channel_post(
        db, channel_id=ch.id, created_by=1, status="sent", text="old", buttons_json="[]"
    )
    await store.update_channel_post(
        db,
        prev.id,
        tg_message_id=55,
        tg_message_ids_json="[55, 56, 57]",
        sent_at="2026-01-01T00:00:00+00:00",
    )
    post = _album_post(ch.id, id=2, delete_previous=True)
    bot = FakeSendBot()
    await svc.send_post(bot, db, post, ch)
    assert bot.deleted == [(-100123, 55), (-100123, 56), (-100123, 57)]


async def test_dispatch_recurring_album_sends_and_records_ids(db):
    ch = await _channel(db)
    now = datetime.now(UTC)
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="recurring",
        text="rec",
        media_type="album",
        media_json=json.dumps(
            [{"type": "photo", "file_id": "A"}, {"type": "photo", "file_id": "B"}]
        ),
        buttons_json="[]",
        scheduled_at=now.isoformat(),
        recurrence="daily",
        recur_at="09:00",
    )
    bot = FakeSendBot()
    assert await svc.dispatch_due_posts(bot, db) == 1
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "recurring"
    assert updated.tg_message_id is not None
    assert json.loads(updated.tg_message_ids_json) == [
        m.message_id for m in bot.sent[0]["media_group_ids"]
    ]
    assert svc.parse_dt(updated.scheduled_at) > now


async def test_dispatch_expires_album_deletes_all_messages(db):
    ch = await _channel(db)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="sent",
        text="gone",
        media_type="album",
        media_json=json.dumps(
            [{"type": "photo", "file_id": "A"}, {"type": "photo", "file_id": "B"}]
        ),
    )
    await store.update_channel_post(
        db, post.id, tg_message_id=70, tg_message_ids_json="[70, 71, 72]", expires_at=past
    )
    bot = FakeSendBot()
    await svc.dispatch_due_posts(bot, db)
    assert bot.deleted == [(-100123, 70), (-100123, 71), (-100123, 72)]
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "expired"
    assert updated.tg_message_id is None
    assert updated.tg_message_ids_json is None
    assert updated.expires_at is None


async def test_edit_published_album_caption(db):
    ch = await _channel(db)
    post = _album_post(ch.id, tg_message_id=42, tg_message_ids_json="[42, 43, 44]")

    edits: list[dict] = []

    class AlbumEditBot:
        async def edit_message_caption(self, **kwargs):
            # real signature: no link_preview_options; albums carry no keyboard
            assert "link_preview_options" not in kwargs
            assert "reply_markup" not in kwargs
            edits.append(kwargs)

    assert await svc.edit_published_post(AlbumEditBot(), post, ch) is True
    assert edits[0]["caption"] == "کپشن آلبوم"
    assert edits[0]["message_id"] == 42


async def test_send_preview_album(db):
    ch = await _channel(db)
    post = _album_post(ch.id)
    bot = FakeSendBot()
    await svc.send_preview(bot, 777, post)
    assert bot.sent[0]["chat_id"] == 777
    assert len(bot.sent[0]["media"]) == 3


# ── presentation ─────────────────────────────────────────────────────────────


def test_post_summary_line_recurring():
    post = _make_post(
        1, status="recurring", recurrence="weekly", recur_at="09:00", text="سلام\nدنیا"
    )
    line = svc.post_summary_line(post)
    assert line.startswith("🔁 #1")
    assert "هر هفته 09:00" in line


def test_post_summary_line_escapes_html():
    post = _make_post(1, status="sent", text="<b>bold</b>", sent_at="2026-08-27T00:00:00+00:00")
    line = svc.post_summary_line(post)
    assert "<b>" not in line  # escaped
    assert "&lt;b&gt;bold&lt;/b&gt;" in line
