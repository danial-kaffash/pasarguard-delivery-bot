"""Tests for /backup, /restore, /export, /import command handlers (bot/handlers/backup.py)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bot.handlers.backup import (
    _build_export,
    _is_valid_sqlite,
    cmd_backup,
    cmd_export,
    cmd_import,
    cmd_restore,
)
from storage import db as store
from tests.helpers import FakeFileBot, make_settings


@pytest.fixture
async def db(tmp_path):
    conn = await store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        try:
            await conn.close()
        except Exception:
            pass  # restore tests close the connection themselves


class FakeDocMessage:
    """Message stand-in with document/reply support that records answers."""

    def __init__(self, document=None, reply_document=None, user_id: int = 1):
        self.document = document
        self.reply_to_message = SimpleNamespace(document=reply_document) if reply_document else None
        self.from_user = SimpleNamespace(id=user_id, first_name="Owner", username="owner")
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.answers: list[tuple[str, dict]] = []
        self.documents: list[dict] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.answers))

    async def answer_document(self, document=None, caption: str | None = None, **kwargs):
        self.documents.append({"document": document, "caption": caption, **kwargs})
        return SimpleNamespace(message_id=1)

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.answers]


def _doc(file_id: str = "f1", file_name: str = "backup.db"):
    return SimpleNamespace(file_id=file_id, file_name=file_name)


def _settings_for(tmp_path):
    s = make_settings()
    s.db_path = str(tmp_path / "test.db")
    return s


def _sqlite_bytes(tmp_path, name: str = "fresh.db") -> bytes:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return path.read_bytes()


async def _seed(db):
    panel = await store.create_panel(
        db,
        name="P",
        base_url="https://p",
        admin_username="a",
        admin_password="b",
    )
    ch = await store.create_channel(db, tg_channel_id=-100123, title="Test")
    await store.upsert_user(db, tg_user_id=1, role="superadmin")
    await store.upsert_channel_offer_group(
        db, channel_id=ch.id, panel_id=panel.id, group_id=2, label="NL"
    )
    return panel, ch


# ── _is_valid_sqlite ─────────────────────────────────────────────────────────


def test_is_valid_sqlite_rejects_short_data():
    assert _is_valid_sqlite(b"SQLite") is False


def test_is_valid_sqlite_rejects_wrong_magic():
    assert _is_valid_sqlite(b"Not a sqlite file" + b"x" * 16) is False


def test_is_valid_sqlite_accepts_real_database(tmp_path):
    assert _is_valid_sqlite(_sqlite_bytes(tmp_path)) is True


def test_is_valid_sqlite_rejects_header_with_corrupt_body(tmp_path):
    data = b"SQLite format 3\x00" + b"garbage" * 8
    assert _is_valid_sqlite(data) is False


# ── /backup ──────────────────────────────────────────────────────────────────


async def test_backup_missing_file_answers_error(tmp_path, db):
    msg = FakeDocMessage()
    settings = _settings_for(tmp_path)
    settings.db_path = str(tmp_path / "missing.db")
    await cmd_backup(msg, db=db, settings=settings)
    assert "یافت نشد" in msg.texts[0]


async def test_backup_sends_database_file(tmp_path, db):
    await _seed(db)
    msg = FakeDocMessage()
    settings = _settings_for(tmp_path)
    await cmd_backup(msg, db=db, settings=settings)
    assert len(msg.documents) == 1
    assert msg.documents[0]["document"].data[:15] == b"SQLite format 3"
    assert "pasarguard_backup_" in msg.documents[0]["document"].filename


async def test_backup_send_failure_answers_error(tmp_path, db, monkeypatch):
    def broken_read(self):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr("pathlib.Path.read_bytes", broken_read)
    msg = FakeDocMessage()
    settings = _settings_for(tmp_path)
    await cmd_backup(msg, db=db, settings=settings)
    assert "خطا" in msg.texts[0]


# ── /export ──────────────────────────────────────────────────────────────────


async def test_export_builds_complete_payload(db):
    panel, ch = await _seed(db)
    data = await _build_export(db)
    assert data["version"] == 1
    assert data["exported_at"].startswith(datetime.now(UTC).strftime("%Y"))
    assert len(data["panels"]) == 1
    assert data["panels"][0]["name"] == "P"
    assert len(data["channels"]) == 1
    assert data["channels"][0]["tg_channel_id"] == -100123
    assert data["users"][0] == {"tg_user_id": 1, "role": "superadmin"}
    # Offer group references its panel by index in the panels list.
    assert data["channel_offer_groups"][0]["panel_index"] == 0
    assert data["channel_offer_groups"][0]["group_id"] == 2


async def test_cmd_export_sends_json_document(db):
    await _seed(db)
    msg = FakeDocMessage()
    await cmd_export(msg, db=db)
    assert len(msg.documents) == 1
    doc = msg.documents[0]
    payload = json.loads(doc["document"].data.decode("utf-8"))
    assert payload["version"] == 1
    assert "pasarguard_config_" in doc["document"].filename
    assert "پنل‌ها: 1" in doc["caption"]


async def test_cmd_export_failure_answers_error(db, monkeypatch):
    monkeypatch.setattr(
        "bot.handlers.backup._build_export",
        _raise_export_error,
    )
    msg = FakeDocMessage()
    await cmd_export(msg, db=db)
    assert "خطا" in msg.texts[0]


async def _raise_export_error(db):
    raise RuntimeError("export boom")


# ── /restore ─────────────────────────────────────────────────────────────────


async def test_restore_without_document_shows_instructions(db):
    msg = FakeDocMessage()
    await cmd_restore(msg, bot=FakeFileBot(), db=db, settings=make_settings())
    assert "بکاپ" in msg.texts[0]


async def test_restore_rejects_non_db_extension(db):
    msg = FakeDocMessage(document=_doc(file_name="backup.json"))
    await cmd_restore(msg, bot=FakeFileBot(), db=db, settings=make_settings())
    assert ".db" in msg.texts[0]


async def test_restore_download_failure_answers_error(db):
    msg = FakeDocMessage(document=_doc())
    bot = FakeFileBot()  # no files registered → get_file raises
    await cmd_restore(msg, bot=bot, db=db, settings=make_settings())
    assert "دانلود" in msg.texts[0]


async def test_restore_rejects_non_sqlite_content(tmp_path, db):
    msg = FakeDocMessage(document=_doc())
    bot = FakeFileBot(files={"f1": b"definitely not a database" * 4})
    await cmd_restore(msg, bot=bot, db=db, settings=make_settings())
    assert "معتبر نیست" in msg.texts[0]


async def test_restore_replaces_database_and_execs_restart(tmp_path, db, monkeypatch):
    fresh = _sqlite_bytes(tmp_path, "fresh.db")
    db_path = tmp_path / "test.db"
    assert db_path.exists()

    def fake_execv(path, argv):
        raise RuntimeError("EXECV-CALLED")

    monkeypatch.setattr("os.execv", fake_execv)
    msg = FakeDocMessage(document=_doc())
    bot = FakeFileBot(files={"f1": fresh})
    settings = _settings_for(tmp_path)

    with pytest.raises(RuntimeError, match="EXECV-CALLED"):
        await cmd_restore(msg, bot=bot, db=db, settings=settings)

    # Database file replaced with the uploaded bytes; a pre-restore backup was kept.
    assert db_path.read_bytes() == fresh
    backups = list(tmp_path.glob("test.db.pre-restore.*"))
    assert len(backups) == 1
    assert "بازیابی شد" in msg.texts[0]


async def test_restore_write_failure_answers_error(tmp_path, db, monkeypatch):
    fresh = _sqlite_bytes(tmp_path, "fresh2.db")

    def fake_write(self, data):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", fake_write)
    msg = FakeDocMessage(document=_doc())
    bot = FakeFileBot(files={"f1": fresh})
    settings = _settings_for(tmp_path)

    await cmd_restore(msg, bot=bot, db=db, settings=settings)
    assert "ذخیره فایل" in msg.texts[0]


# ── /import ──────────────────────────────────────────────────────────────────


def _import_payload(**overrides) -> dict:
    data = {
        "version": 1,
        "panels": [
            {
                "name": "P",
                "base_url": "https://p",
                "admin_username": "a",
                "verify_ssl": True,
                "timeout_seconds": 15.0,
                "protocols": "vless",
                "auto_delete_days": 11,
                "active": True,
            }
        ],
        "channels": [
            {
                "tg_channel_id": -100999,
                "title": "",
                "trial_data_limit_gb": 5.0,
                "trial_days": 3,
                "on_hold_grace_days": 7,
                "allow_regrant_after_days": 30,
                "trial_max_member_age_days": 0,
                "join_approval_delay_seconds": 10,
                "promo_interval_hours": 6.0,
                "promo_pin": True,
                "promo_silent": True,
                "active": True,
            }
        ],
        "users": [{"tg_user_id": 55, "role": "admin"}],
        "channel_admins": [{"tg_user_id": 55, "channel_tg_id": -100999}],
        "channel_offer_groups": [
            {"channel_tg_id": -100999, "panel_index": 0, "group_id": 4, "label": "🇳🇱 هلند"}
        ],
        "settings": {"paused": "false"},
    }
    data.update(overrides)
    return data


async def test_import_without_document_shows_instructions(db):
    msg = FakeDocMessage()
    await cmd_import(msg, bot=FakeFileBot(), db=db)
    assert "JSON" in msg.texts[0]


async def test_import_rejects_non_json_extension(db):
    msg = FakeDocMessage(document=_doc(file_name="backup.db"))
    await cmd_import(msg, bot=FakeFileBot(), db=db)
    assert ".json" in msg.texts[0]


async def test_import_download_failure_answers_error(db):
    msg = FakeDocMessage(document=_doc(file_name="cfg.json"))
    bot = FakeFileBot()  # get_file raises
    await cmd_import(msg, bot=bot, db=db)
    assert "خواندن فایل" in msg.texts[0]


async def test_import_rejects_wrong_version(db):
    payload = json.dumps({"version": 99}).encode("utf-8")
    msg = FakeDocMessage(document=_doc(file_name="cfg.json"))
    bot = FakeFileBot(files={"f1": payload})
    await cmd_import(msg, bot=bot, db=db)
    assert "نسخه فایل" in msg.texts[0]


async def test_import_applies_full_configuration(db):
    payload = json.dumps(_import_payload(), ensure_ascii=False).encode("utf-8")
    msg = FakeDocMessage(document=_doc(file_name="cfg.json"))
    bot = FakeFileBot(files={"f1": payload}, chat_titles={-100999: "Imported Channel"})

    await cmd_import(msg, bot=bot, db=db)

    panels = await store.list_panels(db, active_only=False)
    assert len(panels) == 1 and panels[0].name == "P"
    ch = await store.get_channel_by_tg_id(db, -100999)
    assert ch.title == "Imported Channel"  # fetched via get_chat (title was empty)
    user = await store.get_user(db, 55)
    assert user.role == "admin"
    admins = await store.list_channel_admins(db, ch.id)
    assert any(a.tg_user_id == 55 for a in admins)
    offers = await store.list_channel_offer_groups(db, ch.id)
    assert len(offers) == 1 and offers[0].label == "🇳🇱 هلند"
    assert await store.get_setting(db, "paused") == "false"
    # The summary message reports every applied section.
    text = msg.texts[0]
    assert "پنل‌ها: 1" in text and "کانال‌ها: 1" in text and "گروه‌ها: 1" in text


async def test_import_upserts_existing_records(db):
    await store.create_panel(
        db, name="P", base_url="https://old", admin_username="a", admin_password="b"
    )
    await store.create_channel(db, tg_channel_id=-100999, title="Existing")

    payload = json.dumps(_import_payload(), ensure_ascii=False).encode("utf-8")
    msg = FakeDocMessage(document=_doc(file_name="cfg.json"))
    bot = FakeFileBot(files={"f1": payload}, chat_titles={-100999: "Refetched Title"})

    await cmd_import(msg, bot=bot, db=db)

    panels = await store.list_panels(db, active_only=False)
    assert len(panels) == 1  # updated, not duplicated
    assert panels[0].base_url == "https://p"
    ch = await store.get_channel_by_tg_id(db, -100999)
    # Import overwrote the title with the payload's empty title, then re-fetched
    # it from Telegram (titles are only kept when non-empty in the payload).
    assert ch.title == "Refetched Title"


async def test_import_soft_deletes_inactive_channel(db):
    payload = dict(_import_payload())
    payload["channels"] = [dict(payload["channels"][0], active=False)]
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    msg = FakeDocMessage(document=_doc(file_name="cfg.json"))
    bot = FakeFileBot(files={"f1": data})

    await cmd_import(msg, bot=bot, db=db)

    active = await store.list_channels(db, active_only=True)
    assert active == []
    all_channels = await store.list_channels(db, active_only=False)
    assert len(all_channels) == 1  # kept but soft-deleted


async def test_import_failure_answers_error(db, monkeypatch):
    monkeypatch.setattr(
        "bot.handlers.backup._apply_import",
        _raise_import_error,
    )
    payload = json.dumps(_import_payload(), ensure_ascii=False).encode("utf-8")
    msg = FakeDocMessage(document=_doc(file_name="cfg.json"))
    bot = FakeFileBot(files={"f1": payload})
    await cmd_import(msg, bot=bot, db=db)
    assert "خطا در بازیابی" in msg.texts[0]


async def _raise_import_error(db, bot, data):
    raise RuntimeError("import boom")
