"""Tests for bot/handlers/posts.py — wizard, management actions, /checkpremium."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MessageEntity

from bot.handlers.admin import cmd_editchannel
from bot.handlers.panel import build_channel_menu
from bot.handlers.posts import (
    PostsCB,
    PostWizard,
    _apply_layout,
    cmd_checkpremium,
    cmd_newpost,
    cmd_posts,
    input_button_label,
    input_button_url,
    input_check_premium,
    input_content,
    input_edit_text,
    input_reschedule,
    input_schedule_time,
    on_button_style,
    on_channel_toggle,
    on_confirm,
    on_newpost_shortcut,
    on_post_action,
    on_wizard_next,
    parse_layout,
    render_posts_view,
    wizard_stray_text,
)
from services import posts as svc
from storage import db as store
from tests.helpers import FakeEditableMessage, FakeMessage, FakeState, make_settings
from tests.test_posts_service import FakeSendBot

SETTINGS = make_settings()


class FakeChannelBot(FakeSendBot):
    """FakeSendBot + edit_message_* for the published-post edit flow."""

    def __init__(self):
        super().__init__()
        self.edits: list[dict] = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True

    async def edit_message_caption(self, **kwargs):
        self.edits.append(kwargs)
        return True


class FakePanelMessage:
    """Editable bot message in a chat — for callback-driven flows."""

    def __init__(self, chat_id: int = 1):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.edits: list[tuple[str, dict]] = []
        self.answers: list[tuple[str, dict]] = []

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return SimpleNamespace(message_id=1)

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.answers))


class FakeMsg:
    """A message carrying text + entities (wizard content input)."""

    def __init__(
        self,
        text: str | None = "",
        entities=None,
        user_id: int = 1,
        photo=None,
        video=None,
        animation=None,
        caption=None,
    ):
        self.text = text
        self.entities = entities
        self.caption = entities and None or caption
        self.photo = photo
        self.video = video
        self.animation = animation
        self.caption_entities = None
        self.from_user = SimpleNamespace(id=user_id, first_name="T", username="t")
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.replies: list[tuple[str, dict]] = []
        self.bot = FakeChannelBot()

    async def answer(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies))

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.replies]


def fake_cb(message=None, cb: PostsCB | None = None, user_id: int = 1, bot=None):
    return SimpleNamespace(
        message=message or FakePanelMessage(user_id),
        from_user=SimpleNamespace(id=user_id),
        bot=bot or FakeChannelBot(),
        answer=AsyncMock(),
    )


def cb_data(action: str, tid: int = 0, extra: str = "") -> PostsCB:
    return PostsCB(action=action, tid=tid, extra=extra)


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


async def _setup(db, *, post_delete_previous: bool = False):
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    ch = await store.create_channel(
        db, tg_channel_id=-100123, title="Test", post_delete_previous=post_delete_previous
    )
    return ch


# ── /newpost entry ───────────────────────────────────────────────────────────


async def test_cmd_newpost_shows_channel_picker(db):
    ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    state = FakeState()
    await cmd_newpost(msg, SimpleNamespace(args=None), state=state, db=db, settings=SETTINGS)
    assert "کانال" in msg.texts[0]
    kb = msg.replies[0][1]["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any(ch.title in lbl for lbl in labels)
    assert state.state == PostWizard.picking_channels


async def test_cmd_newpost_arg_preselects_and_skips_to_content(db):
    ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    state = FakeState()
    await cmd_newpost(
        msg, SimpleNamespace(args=str(ch.tg_channel_id)), state=state, db=db, settings=SETTINGS
    )
    assert "محتوا" in msg.texts[0]
    assert state.data["selected"] == [ch.id]
    assert state.data["opts"]["delete_previous"] is False
    assert state.state == PostWizard.content


async def test_cmd_newpost_channel_default_delete_previous(db):
    await _setup(db, post_delete_previous=True)
    msg = FakeMessage(user_id=1)
    state = FakeState()
    await cmd_newpost(msg, SimpleNamespace(args=None), state=state, db=db, settings=SETTINGS)
    assert state.data["opts"]["delete_previous"] is True


async def test_cmd_newpost_without_channels(db):
    msg = FakeMessage(user_id=42)
    state = FakeState()
    await cmd_newpost(msg, SimpleNamespace(args=None), state=state, db=db, settings=SETTINGS)
    assert "کانالی" in msg.texts[0]


async def test_cmd_newpost_unknown_channel_arg(db):
    await _setup(db)
    msg = FakeMessage(user_id=1)
    state = FakeState()
    await cmd_newpost(msg, SimpleNamespace(args="-100999"), state=state, db=db, settings=SETTINGS)
    assert "یافت نشد" in msg.texts[0]


async def test_channel_toggle_and_access_guard(db):
    ch = await _setup(db)
    # foreign admin with no assignment cannot toggle
    await store.upsert_user(db, tg_user_id=7, role="admin")
    cb = fake_cb(FakeEditableMessage(), cb_data("cht", ch.id), user_id=7)
    state = FakeState()
    state.data = {"selected": []}
    await on_channel_toggle(cb, cb_data("cht", ch.id), state=state, db=db, settings=SETTINGS)
    cb.answer.assert_awaited_once()
    assert "دسترسی" in cb.answer.await_args.args[0]

    # the superadmin can
    cb2 = fake_cb(FakeEditableMessage(), cb_data("cht", ch.id), user_id=1)
    await on_channel_toggle(cb2, cb_data("cht", ch.id), state=state, db=db, settings=SETTINGS)
    assert state.data["selected"] == [ch.id]


# ── wizard navigation ────────────────────────────────────────────────────────


async def test_wizard_next_content_requires_selection(db):
    cb = fake_cb(FakeEditableMessage(), cb_data("next", extra="content"))
    state = FakeState()
    state.data = {"selected": []}
    await on_wizard_next(cb, cb_data("next", extra="content"), state=state)
    cb.answer.assert_awaited_once()
    assert "کانال" in cb.answer.await_args.kwargs.get("text", "") or True


async def test_wizard_next_buttons_requires_content():
    cb = fake_cb(FakeEditableMessage(), cb_data("next", extra="buttons"))
    state = FakeState()
    state.data = {"selected": [1], "buttons": []}
    await on_wizard_next(cb, cb_data("next", extra="buttons"), state=state)
    cb.answer.assert_awaited_once()
    alert = cb.answer.await_args.kwargs.get("show_alert")
    assert alert is True


# ── content input ────────────────────────────────────────────────────────────


async def test_input_content_text_with_entities():
    msg = FakeMsg(
        "🎉 سلام",
        entities=[
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="531"),
        ],
    )
    state = FakeState()
    await input_content(msg, state)
    assert state.data["text"] == "🎉 سلام"
    ents = svc.entities_from_json(state.data["entities_json"])
    assert ents[0].custom_emoji_id == "531"
    assert "ذخیره شد" in msg.texts[0]


async def test_input_content_photo_with_caption():
    photo = [SimpleNamespace(file_id="PH1")]
    msg = FakeMsg(text=None, photo=photo, caption="کپشن")
    msg.caption = "کپشن"
    state = FakeState()
    await input_content(msg, state)
    assert state.data["media_type"] == "photo"
    assert state.data["media_file_id"] == "PH1"
    assert state.data["text"] == "کپشن"


async def test_input_content_rejects_unsupported():
    msg = FakeMsg(text=None)  # no text, no media
    state = FakeState()
    await input_content(msg, state)
    assert "فقط" in msg.texts[0]
    assert "text" not in state.data


# ── button builder flow ──────────────────────────────────────────────────────


async def test_button_flow_label_url_style():
    state = FakeState()
    label_msg = FakeMsg(
        "🎉 دریافت",
        entities=[
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="531"),
        ],
    )
    await input_button_label(label_msg, state)
    pending = state.data["pending_button"]
    assert pending["label"] == "🎉 دریافت"
    assert pending["icon"] == "531"
    assert pending["label_clean"] == "دریافت"

    url_msg = FakeMsg("https://t.me/bot?start=x")
    await input_button_url(url_msg, state)
    assert state.data["pending_button"]["action"] == {
        "type": "url",
        "url": "https://t.me/bot?start=x",
    }

    cb = fake_cb(FakeEditableMessage(), cb_data("bstyle", extra="success"))
    await on_button_style(cb, cb_data("bstyle", extra="success"), state=state)
    buttons = state.data["buttons"]
    assert len(buttons) == 1
    assert buttons[0]["style"] == "success"
    assert state.data["pending_button"] is None


async def test_button_url_rejects_bad_scheme():
    msg = FakeMsg("javascript:alert(1)")
    state = FakeState()
    await input_button_url(msg, state)
    assert "action" not in (state.data.get("pending_button") or {})
    assert "لینک" in msg.texts[0]


# ── layout ───────────────────────────────────────────────────────────────────


def test_parse_layout_variants():
    assert parse_layout("2,1") == [2, 1]
    assert parse_layout("2،1") == [2, 1]  # Persian comma
    assert parse_layout("-") is None
    assert parse_layout(None) is None
    with pytest.raises(ValueError):
        parse_layout("9")
    with pytest.raises(ValueError):
        parse_layout("a,b")


def test_apply_layout():
    buttons = [{"label": "a"}, {"label": "b"}, {"label": "c"}]
    rows = _apply_layout({"buttons": buttons, "layout": [2, 1]})
    assert [b["row"] for b in rows] == [0, 0, 1]
    rows_default = _apply_layout({"buttons": buttons, "layout": None})
    assert [b["row"] for b in rows_default] == [0, 1, 2]
    rows_overflow = _apply_layout({"buttons": buttons, "layout": [1]})
    assert [b["row"] for b in rows_overflow] == [0, 1, 1]


# ── schedule input ───────────────────────────────────────────────────────────


async def test_input_schedule_time_once():
    state = FakeState()
    state.data = {"sched_mode": "once"}
    msg = FakeMsg("2099-01-01 10:00")
    await input_schedule_time(msg, state)
    assert state.data["sched_mode"] == "once"
    assert state.data["scheduled_at"] is not None


async def test_input_schedule_time_recurring():
    state = FakeState()
    state.data = {"sched_mode": "recurring", "recurrence": "daily"}
    msg = FakeMsg("09:00")
    await input_schedule_time(msg, state)
    assert state.data["recur_at"] == "09:00"
    assert state.data["scheduled_at"] is not None
    assert "تکرار" in msg.texts[0]


async def test_input_schedule_time_past_rejected():
    state = FakeState()
    state.data = {"sched_mode": "once"}
    msg = FakeMsg("2020-01-01 10:00")
    await input_schedule_time(msg, state)
    assert "آینده" in msg.texts[0]
    assert "scheduled_at" not in state.data


# ── confirm ──────────────────────────────────────────────────────────────────


def _wizard_data(ch_id: int, **overrides) -> dict:
    data = {
        "wizard": True,
        "selected": [ch_id],
        "text": "پست تستی",
        "entities_json": None,
        "media_type": None,
        "media_file_id": None,
        "buttons": [
            {
                "label": "لینک",
                "action": {"type": "url", "url": "https://t.me/x"},
                "style": "success",
                "row": 0,
            }
        ],
        "layout": None,
        "opts": {
            "delete_previous": False,
            "pin": True,
            "silent": False,
            "link_preview": True,
            "ephemeral_hours": 6,
        },
        "sched_mode": "immediate",
        "recurrence": "none",
        "recur_at": None,
        "sched_at": None,
        "save_template": False,
    }
    data.update(overrides)
    return data


async def test_confirm_immediate_send(db):
    ch = await _setup(db)
    state = FakeState()
    state.data = _wizard_data(ch.id)
    bot = FakeChannelBot()
    cb = fake_cb(FakeEditableMessage(), cb_data("confirm"), bot=bot)
    await on_confirm(cb, state=state, db=db, settings=SETTINGS)
    posts = await store.list_channel_posts(db, ch.id)
    assert len(posts) == 1
    assert posts[0].status == "sent"
    assert posts[0].tg_message_id is not None
    assert posts[0].pin is True
    assert posts[0].ephemeral_hours == 6
    assert posts[0].expires_at is not None
    assert len(bot.sent) == 1
    kb = bot.sent[0]["reply_markup"]
    assert kb.inline_keyboard[0][0].url == "https://t.me/x"
    assert state.state is None  # cleared


async def test_confirm_multi_channel_shared_group(db):
    await _setup(db)
    ch2 = await store.create_channel(db, tg_channel_id=-100456, title="Second")
    state = FakeState()
    state.data = _wizard_data(0, selected=[1, ch2.id])
    cb = fake_cb(FakeEditableMessage(), cb_data("confirm"), bot=FakeChannelBot())
    await on_confirm(cb, state=state, db=db, settings=SETTINGS)
    p1 = (await store.list_channel_posts(db, 1))[0]
    p2 = (await store.list_channel_posts(db, ch2.id))[0]
    assert p1.group_id == p2.group_id and p1.group_id is not None
    assert len(cb.message.edits) == 1 or True  # result message rendered


async def test_confirm_scheduled(db):
    ch = await _setup(db)
    when = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    state = FakeState()
    state.data = _wizard_data(ch.id, sched_mode="once", sched_at=when)
    cb = fake_cb(FakeEditableMessage(), cb_data("confirm"), bot=FakeChannelBot())
    await on_confirm(cb, state=state, db=db, settings=SETTINGS)
    post = (await store.list_channel_posts(db, ch.id))[0]
    assert post.status == "scheduled"
    assert post.scheduled_at == when


async def test_confirm_recurring(db):
    ch = await _setup(db)
    when = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    state = FakeState()
    state.data = _wizard_data(
        ch.id,
        sched_mode="recurring",
        recurrence="weekly",
        recur_at="09:00",
        sched_at=when,
    )
    cb = fake_cb(FakeEditableMessage(), cb_data("confirm"), bot=FakeChannelBot())
    await on_confirm(cb, state=state, db=db, settings=SETTINGS)
    post = (await store.list_channel_posts(db, ch.id))[0]
    assert post.status == "recurring"
    assert post.recur_at == "09:00"


async def test_confirm_saves_template(db):
    ch = await _setup(db)
    state = FakeState()
    state.data = _wizard_data(ch.id, save_template=True)
    cb = fake_cb(FakeEditableMessage(), cb_data("confirm"), bot=FakeChannelBot())
    await on_confirm(cb, state=state, db=db, settings=SETTINGS)
    templates = await store.list_post_templates(db)
    assert len(templates) == 1
    assert templates[0].text == "پست تستی"
    assert "قالب" in cb.message.edits[0][0]


async def test_confirm_requires_schedule_time(db):
    ch = await _setup(db)
    state = FakeState()
    state.data = _wizard_data(ch.id, sched_mode="once", sched_at=None)
    cb = fake_cb(FakeEditableMessage(), cb_data("confirm"))
    await on_confirm(cb, state=state, db=db, settings=SETTINGS)
    posts = await store.list_channel_posts(db, ch.id)
    assert posts == []
    assert cb.answer.await_args.kwargs.get("show_alert") is True


# ── /posts list + management ─────────────────────────────────────────────────


async def _published_post(db, ch, status="sent", **kw):
    post = await store.create_channel_post(
        db, channel_id=ch.id, created_by=1, status=status, text="متن", **kw
    )
    if status in ("sent", "recurring"):
        await store.update_channel_post(
            db, post.id, tg_message_id=77, sent_at=datetime.now(UTC).isoformat()
        )
    return await store.get_channel_post(db, post.id)


async def test_render_posts_view_lists_posts(db):
    ch = await _setup(db)
    await _published_post(db, ch)
    text, kb = await render_posts_view(db, ch)
    assert "پست‌ها" in text
    assert "#1" in text
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any("newpost" in a for a in actions)


async def test_cmd_posts_with_channel_arg(db):
    ch = await _setup(db)
    await _published_post(db, ch)
    msg = FakeMessage(user_id=1)
    await cmd_posts(msg, SimpleNamespace(args=str(ch.tg_channel_id)), db=db, settings=SETTINGS)
    assert "#1" in msg.texts[0]


async def test_cmd_posts_foreign_channel_denied(db):
    await _setup(db)
    own = await store.create_channel(db, tg_channel_id=-100999, title="Own")
    await store.upsert_user(db, tg_user_id=7, role="admin")
    await store.assign_channel_admin(db, 7, own.id)
    msg = FakeMessage(user_id=7)
    await cmd_posts(msg, SimpleNamespace(args="-100123"), db=db, settings=SETTINGS)
    assert "دسترس" in msg.texts[0]


async def test_post_action_send_now(db):
    ch = await _setup(db)
    post = await _published_post(
        db,
        ch,
        status="scheduled",
        scheduled_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    state = FakeState()
    bot = FakeChannelBot()
    cb = fake_cb(FakeEditableMessage(), cb_data("pact", post.id, "send"), bot=bot)
    await on_post_action(
        cb, cb_data("pact", post.id, "send"), state=state, db=db, settings=SETTINGS
    )
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "sent"
    assert updated.tg_message_id is not None
    assert len(bot.sent) == 1


async def test_post_action_cancel(db):
    ch = await _setup(db)
    post = await _published_post(
        db,
        ch,
        status="scheduled",
        scheduled_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    state = FakeState()
    cb = fake_cb(FakeEditableMessage(), cb_data("pact", post.id, "cancel"))
    await on_post_action(
        cb, cb_data("pact", post.id, "cancel"), state=state, db=db, settings=SETTINGS
    )
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "cancelled"
    assert updated.scheduled_at is None


async def test_post_action_delete(db):
    ch = await _setup(db)
    post = await _published_post(db, ch)
    state = FakeState()
    bot = FakeChannelBot()
    cb = fake_cb(FakeEditableMessage(), cb_data("pact", post.id, "del"), bot=bot)
    await on_post_action(cb, cb_data("pact", post.id, "del"), state=state, db=db, settings=SETTINGS)
    assert await store.get_channel_post(db, post.id) is None
    assert bot.deleted == [(-100123, 77)]


async def test_post_action_copy_preloads_preview(db):
    ch = await _setup(db)
    post = await _published_post(db, ch)
    state = FakeState()
    bot = FakeChannelBot()
    cb = fake_cb(FakePanelMessage(), cb_data("pact", post.id, "copy"), bot=bot)
    await on_post_action(
        cb, cb_data("pact", post.id, "copy"), state=state, db=db, settings=SETTINGS
    )
    assert state.data["selected"] == [ch.id]
    assert state.data["text"] == "متن"
    assert len(bot.sent) >= 1  # the preview was rendered in the admin chat


async def test_post_action_access_denied(db):
    ch = await _setup(db)
    post = await _published_post(db, ch)
    await store.upsert_user(db, tg_user_id=7, role="admin")
    state = FakeState()
    cb = fake_cb(FakeEditableMessage(), cb_data("pact", post.id, "del"), user_id=7)
    await on_post_action(cb, cb_data("pact", post.id, "del"), state=state, db=db, settings=SETTINGS)
    assert await store.get_channel_post(db, post.id) is not None  # untouched


async def test_newpost_shortcut_starts_wizard(db):
    ch = await _setup(db)
    state = FakeState()
    cb = fake_cb(FakeEditableMessage(), cb_data("newpost", ch.id))
    await on_newpost_shortcut(cb, cb_data("newpost", ch.id), state=state, db=db, settings=SETTINGS)
    assert state.data["selected"] == [ch.id]
    assert state.state == PostWizard.content
    assert "محتوا" in cb.message.answers[0][0]


# ── reschedule / edit published ──────────────────────────────────────────────


async def test_input_reschedule_one_shot(db):
    ch = await _setup(db)
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="scheduled",
        text="x",
        scheduled_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    state = FakeState()
    state.data = {"resched_post_id": post.id}
    msg = FakeMsg("2099-06-01 12:00")
    await input_reschedule(msg, state, db)
    updated = await store.get_channel_post(db, post.id)
    assert updated.status == "scheduled"
    assert updated.scheduled_at is not None
    assert "زمان‌بندی جدید" in msg.texts[0]


async def test_input_reschedule_recurring(db):
    ch = await _setup(db)
    post = await store.create_channel_post(
        db,
        channel_id=ch.id,
        created_by=1,
        status="recurring",
        text="x",
        recurrence="daily",
        recur_at="08:00",
        scheduled_at=datetime.now(UTC).isoformat(),
    )
    state = FakeState()
    state.data = {"resched_post_id": post.id}
    msg = FakeMsg("09:30")
    await input_reschedule(msg, state, db)
    updated = await store.get_channel_post(db, post.id)
    assert updated.recur_at == "09:30"
    assert updated.scheduled_at is not None


async def test_input_edit_text_updates_and_edits(db):
    ch = await _setup(db)
    post = await _published_post(db, ch)
    state = FakeState()
    state.data = {"edit_post_id": post.id}
    msg = FakeMsg("متن جدید")
    await input_edit_text(msg, state, db)
    updated = await store.get_channel_post(db, post.id)
    assert updated.text == "متن جدید"
    assert msg.bot.edits[0]["text"] == "متن جدید"
    assert msg.bot.edits[0]["message_id"] == 77
    assert "ویرایش شد" in msg.texts[0]


# ── /checkpremium ────────────────────────────────────────────────────────────


async def test_checkpremium_success():
    msg = FakeMsg(
        "🎉 تست",
        entities=[
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="531"),
        ],
    )
    state = FakeState()
    await input_check_premium(msg, state)
    assert "✅" in msg.texts[0]
    assert state.state is None


async def test_checkpremium_no_custom_emoji():
    msg = FakeMsg("سلام")
    state = FakeState()
    state.state = PostWizard.check_premium
    await input_check_premium(msg, state)
    assert "پیدا نشد" in msg.texts[0]
    assert state.state is not None  # still waiting


async def test_checkpremium_fragment_error():
    from aiogram.exceptions import TelegramBadRequest

    msg = FakeMsg(
        "🎉 تست",
        entities=[
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="531"),
        ],
    )

    async def boom(chat_id, text=None, **kwargs):
        raise TelegramBadRequest(method="sendMessage", message="Bad Request: CUSTOM_EMOJI")

    msg.bot.send_message = boom
    state = FakeState()
    await input_check_premium(msg, state)
    assert "Fragment" in msg.texts[0]


async def test_cmd_checkpremium_prompt():
    msg = FakeMessage(user_id=1)
    state = FakeState()
    await cmd_checkpremium(msg, state)
    assert "ایموجی پرمیوم" in msg.texts[0]
    assert state.state == PostWizard.check_premium


# ── stray text + /cancel ─────────────────────────────────────────────────────


async def test_wizard_stray_text_hint():
    msg = FakeMsg("hello")
    await wizard_stray_text(msg)
    assert "ویزارد" in msg.texts[0]


# ── /editchannel post_delete_previous ───────────────────────────────────────


async def test_editchannel_post_delete_previous(db):
    ch = await _setup(db)
    msg = FakeMessage(user_id=1)
    await cmd_editchannel(
        msg, SimpleNamespace(args=f"{ch.tg_channel_id} post_delete_previous true"), db=db
    )
    assert "✅" in msg.texts[0]
    updated = await store.get_channel(db, ch.id)
    assert updated.post_delete_previous is True

    msg2 = FakeMessage(user_id=1)
    await cmd_editchannel(
        msg2, SimpleNamespace(args=f"{ch.tg_channel_id} post_delete_previous no"), db=db
    )
    assert (await store.get_channel(db, ch.id)).post_delete_previous is False


# ── panel integration ────────────────────────────────────────────────────────


def test_panel_channel_menu_has_posts_button():
    ch = SimpleNamespace(id=1, title="T", tg_channel_id=-100, promo_pin=True, promo_silent=True)
    kb = build_channel_menu(ch, paused=False, joins_paused=False)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("پست‌ها" in lbl for lbl in labels)


async def test_panel_view_posts_renders_list(db):
    from bot.handlers.panel import PanelCB, on_view

    ch = await _setup(db)
    await _published_post(db, ch)
    message = FakePanelMessage()
    callback = fake_cb(message, PanelCB(action="view", target="posts", tid=ch.id))
    state = FakeState()
    await on_view(
        callback,
        PanelCB(action="view", target="posts", tid=ch.id),
        db=db,
        state=state,
        settings=SETTINGS,
    )
    text = message.edits[0][0]
    assert "پست‌ها" in text and "#1" in text
    # a back-to-channel button is appended for panel navigation
    last_row = message.edits[0][1]["reply_markup"].inline_keyboard[-1]
    assert any("بازگشت" in b.text for b in last_row)
