"""Tests for the inline panel UI callbacks: view/back/confirm/toggle (bot/handlers/panel.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.handlers.panel import PanelCB, on_back, on_confirm, on_toggle, on_view
from storage import db as store
from tests.helpers import FakeCallback, FakePanelManager, make_settings

SETTINGS = make_settings()


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        await conn.close()


class FakeFSMContext:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.state = None
        self.data = {}


async def _setup(db, *, tg_id=-100123):
    panel_row = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=tg_id, title="Test")
    await store.upsert_channel_offer_group(
        db, channel_id=ch.id, panel_id=panel_row.id, group_id=2, label="NL"
    )
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    return panel_row, ch


def _cb(action: str, target: str, tid: int = 0, extra: str = "", user_id: int = 1) -> FakeCallback:
    callback = FakeCallback(user_id=user_id)
    callback.data = PanelCB(action=action, target=target, tid=tid, extra=extra).pack()
    return callback


# ── on_view ──────────────────────────────────────────────────────────────────


async def test_view_channel_detail(db):
    _, ch = await _setup(db)
    cb = _cb("view", "ch", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="ch", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "Test" in cb.message.edits[0][0]
    assert "فعال" in cb.message.edits[0][0]


async def test_view_channel_not_found_answers_alert(db):
    cb = _cb("view", "ch", tid=999)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="ch", tid=999),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert cb.answers[0][0] == "کانال یافت نشد."
    assert cb.message.edits == []


async def test_view_promo_menu(db):
    _, ch = await _setup(db)
    cb = _cb("view", "promo", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="promo", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "پرومو" in cb.message.edits[0][0]


async def test_view_promo_text_shows_current_text(db):
    _, ch = await _setup(db)
    cb = _cb("view", "promo_text", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="promo_text", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "متن فعلی پرومو" in cb.message.edits[0][0]


async def test_view_trial_menu(db):
    _, ch = await _setup(db)
    cb = _cb("view", "trial", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="trial", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "تنظیمات تست" in cb.message.edits[0][0]


async def test_view_join_menu(db):
    _, ch = await _setup(db)
    cb = _cb("view", "join", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="join", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "درخواست عضویت" in cb.message.edits[0][0]


async def test_view_offer_groups_lists_groups(db):
    _, ch = await _setup(db)
    cb = _cb("view", "offer", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="offer", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    text = cb.message.edits[0][0]
    assert "NL" in text
    assert "گروه‌ها" in text


async def test_view_stats(db):
    _, ch = await _setup(db)
    cb = _cb("view", "stats", tid=ch.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="stats", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "آمار" in cb.message.edits[0][0]


async def test_view_backup_menu(db):
    cb = _cb("view", "backup_menu")
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="backup_menu"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "پشتیبانگیری" in cb.message.edits[0][0]


async def test_view_panels_empty(db):
    cb = _cb("view", "panels")
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="panels"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "هیچ پنلی" in cb.message.edits[0][0]


async def test_view_panels_lists_all(db):
    await _setup(db)
    await store.create_panel(
        db, name="P2", base_url="https://p2", admin_username="a", admin_password="b"
    )
    cb = _cb("view", "panels")
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="panels"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "پنل‌ها" in cb.message.edits[0][0]
    assert len(cb.message.edits[0][1]["reply_markup"].inline_keyboard) == 3  # 2 panels + back


async def test_view_panel_detail(db):
    panel_row, _ = await _setup(db)
    cb = _cb("view", "pnl_detail", tid=panel_row.id)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="pnl_detail", tid=panel_row.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    text = cb.message.edits[0][0]
    assert "P" in text and "https://p" in text


async def test_view_main_menu_superadmin(db):
    await _setup(db)
    cb = _cb("view", "main", user_id=1)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="main"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    kb = cb.message.edits[0][1]["reply_markup"]
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("پنل‌ها" in t for t in texts)  # superadmin extras


async def test_view_main_menu_regular_admin_sees_only_assigned(db):
    await _setup(db)
    await store.upsert_user(db, tg_user_id=42, role="admin")
    cb = _cb("view", "main", user_id=42)
    await on_view(
        cb,
        callback_data=PanelCB(action="view", target="main"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    kb = cb.message.edits[0][1]["reply_markup"]
    texts = [btn.text for row in kb.inline_keyboard for btn in row]
    assert not any("مدیریت پنل" in t for t in texts)  # no superadmin extras


# ── on_back ──────────────────────────────────────────────────────────────────


async def test_back_to_main_clears_state(db):
    await _setup(db)
    state = FakeFSMContext()
    state.data = {"channel_id": 1}
    cb = _cb("back", "main")
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target="main"),
        db=db,
        state=state,
        settings=SETTINGS,
    )
    assert state.data == {}  # cleared
    assert cb.message.edits[0][0].startswith("📺")


async def test_back_to_panels(db):
    await _setup(db)
    cb = _cb("back", "panels")
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target="panels"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "پنل‌ها" in cb.message.edits[0][0]


async def test_back_to_panels_empty_shows_back_only(db):
    cb = _cb("back", "panels")
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target="panels"),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    kb = cb.message.edits[0][1]["reply_markup"]
    assert len(kb.inline_keyboard) == 1  # just the back button


async def test_back_to_panel_detail(db):
    panel_row, _ = await _setup(db)
    cb = _cb("back", "pnl_detail", tid=panel_row.id)
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target="pnl_detail", tid=panel_row.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "https://p" in cb.message.edits[0][0]


async def test_back_to_channel(db):
    _, ch = await _setup(db)
    cb = _cb("back", "ch", tid=ch.id)
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target="ch", tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert "Test" in cb.message.edits[0][0]


async def test_back_to_channel_missing_is_noop(db):
    cb = _cb("back", "ch", tid=999)
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target="ch", tid=999),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert cb.message.edits == []
    assert cb.answers[-1][0] is None  # still acked


@pytest.mark.parametrize(
    "target,marker",
    [
        ("promo", "پرومو"),
        ("trial", "تنظیمات تست"),
        ("join", "درخواست عضویت"),
        ("offer", "گروه‌ها"),
    ],
)
async def test_back_to_submenus(db, target, marker):
    _, ch = await _setup(db)
    cb = _cb("back", target, tid=ch.id)
    await on_back(
        cb,
        callback_data=PanelCB(action="back", target=target, tid=ch.id),
        db=db,
        state=FakeFSMContext(),
        settings=SETTINGS,
    )
    assert marker in cb.message.edits[0][0]


# ── on_toggle (remaining targets) ────────────────────────────────────────────


async def test_toggle_promo_silent(db):
    _, ch = await _setup(db)
    assert ch.promo_silent is True
    cb = _cb("toggle", "promo_silent", tid=ch.id)
    await on_toggle(
        cb, callback_data=PanelCB(action="toggle", target="promo_silent", tid=ch.id), db=db
    )
    updated = await store.get_channel(db, ch.id)
    assert updated.promo_silent is False


async def test_toggle_panel_ssl(db):
    panel_row, _ = await _setup(db)
    assert panel_row.verify_ssl is True
    cb = _cb("toggle", "pnl_ssl", tid=panel_row.id)
    await on_toggle(
        cb, callback_data=PanelCB(action="toggle", target="pnl_ssl", tid=panel_row.id), db=db
    )
    updated = await store.get_panel(db, panel_row.id)
    assert updated.verify_ssl is False


async def test_toggle_panel_activate(db):
    panel_row, _ = await _setup(db)
    await store.soft_delete_panel(db, panel_row.id)
    cb = _cb("toggle", "pnl_activate", tid=panel_row.id)
    await on_toggle(
        cb, callback_data=PanelCB(action="toggle", target="pnl_activate", tid=panel_row.id), db=db
    )
    updated = await store.get_panel(db, panel_row.id)
    assert updated.active is True


async def test_toggle_unknown_target_is_noop(db):
    cb = _cb("toggle", "does_not_exist", tid=1)
    await on_toggle(
        cb, callback_data=PanelCB(action="toggle", target="does_not_exist", tid=1), db=db
    )
    assert cb.message.edits == []


# ── on_confirm ───────────────────────────────────────────────────────────────


async def test_confirm_promonow_posts_promo(db, monkeypatch):
    _, ch = await _setup(db)
    posted = AsyncMock()
    monkeypatch.setattr("bot.promo.publish_promo", posted)
    cb = _cb("confirm", "promonow", tid=ch.id)
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="promonow", tid=ch.id),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    posted.assert_awaited_once()
    assert cb.answers[0][1].get("show_alert") is True


async def test_confirm_promonow_failure_alerts(db, monkeypatch):
    _, ch = await _setup(db)

    async def boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("bot.promo.publish_promo", boom)
    cb = _cb("confirm", "promonow", tid=ch.id)
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="promonow", tid=ch.id),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    assert "خطا" in (cb.answers[0][0] or "")


async def test_confirm_offer_del_removes_group(db):
    _, ch = await _setup(db)
    cb = _cb("confirm", "offer_del", tid=ch.id, extra="1_2")
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="offer_del", tid=ch.id, extra="1_2"),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    assert await store.list_channel_offer_groups(db, ch.id) == []
    assert cb.answers[0][0] == "✅ حذف شد."
    assert "خالی" in cb.message.edits[0][0]


async def test_confirm_offer_del_invalid_extra_just_acks(db):
    _, ch = await _setup(db)
    cb = _cb("confirm", "offer_del", tid=ch.id, extra="not_parseable")
    await on_confirm(
        cb,
        callback_data=PanelCB(
            action="confirm", target="offer_del", tid=ch.id, extra="not_parseable"
        ),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    assert await store.list_channel_offer_groups(db, ch.id) != []  # untouched


async def test_confirm_offer_clear(db):
    _, ch = await _setup(db)
    cb = _cb("confirm", "offer_clear", tid=ch.id)
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="offer_clear", tid=ch.id),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    assert await store.list_channel_offer_groups(db, ch.id) == []
    assert cb.answers[0][0] == "✅ همه حذف شدند."


async def test_confirm_panel_remove_soft_deletes(db):
    panel_row, _ = await _setup(db)
    cb = _cb("confirm", "pnl_remove", tid=panel_row.id)
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="pnl_remove", tid=panel_row.id),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    updated = await store.get_panel(db, panel_row.id)
    assert updated.active is False
    assert cb.answers[0][0] == "✅ غیرفعال شد."


async def test_confirm_backup_db_sends_document(tmp_path, db):
    _, ch = await _setup(db)
    settings = make_settings()
    settings.db_path = str(tmp_path / "test.db")
    cb = _cb("confirm", "backup_db")
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="backup_db"),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=settings,
    )
    assert len(cb.message.documents) == 1
    assert cb.message.documents[0]["document"].data[:15] == b"SQLite format 3"


async def test_confirm_backup_db_missing_file_alerts(tmp_path, db):
    settings = make_settings()
    settings.db_path = str(tmp_path / "missing.db")
    cb = _cb("confirm", "backup_db")
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="backup_db"),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=settings,
    )
    assert cb.message.documents == []
    assert "یافت نشد" in (cb.answers[0][0] or "")


async def test_confirm_backup_export_sends_json(db):
    await _setup(db)
    cb = _cb("confirm", "backup_export")
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="backup_export"),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    assert len(cb.message.documents) == 1
    assert "pasarguard_config_" in cb.message.documents[0]["document"].filename


async def test_confirm_unknown_target_just_acks(db):
    cb = _cb("confirm", "whatever")
    await on_confirm(
        cb,
        callback_data=PanelCB(action="confirm", target="whatever"),
        bot=None,
        db=db,
        panel_manager=FakePanelManager(),
        settings=SETTINGS,
    )
    assert cb.answers[-1][0] is None
