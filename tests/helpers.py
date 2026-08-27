"""Shared test fakes: FakeBot, FakePanel, FakeMessage, settings stub."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from panel.models import UserResponse


def make_settings(**overrides):
    base = {
        "trial_data_limit_gb": 5,
        "trial_days": 3,
        "on_hold_grace_days": 7,
        "trial_protocol_list": ["vless"],
        "auto_delete_days": 11,
        "allow_regrant_after_days": 30,
        "channel_id": -1001234567890,
        "promo_interval_hours": 6.0,
        "promo_pin": True,
        "promo_silent": True,
        "owner_tg_ids": [1],
        "trial_max_member_age_days": 0,
        "join_approval_delay_seconds": 10,
    }
    base.update(overrides)
    settings = SimpleNamespace(**base)
    settings.trial_data_limit_bytes = int(base["trial_data_limit_gb"] * 1024**3)
    return settings


def make_panel_user(username: str, sub_url: str = "https://panel.test/sub/abc/") -> UserResponse:
    return UserResponse(
        id=101,
        username=username,
        status="on_hold",
        used_traffic=0,
        created_at=datetime.now(UTC),
        subscription_url=sub_url,
    )


class FakePanel:
    def __init__(self, groups=None, create_side_effects=None):
        self._groups = groups or []
        self.groups_calls = 0
        self.create_calls = []
        self._create_side_effects = list(create_side_effects or [])

    async def list_groups_simple(self):
        self.groups_calls += 1
        return [SimpleNamespace(id=i, name=n) for i, n in self._groups]

    async def create_user(self, user):
        self.create_calls.append(user)
        if self._create_side_effects:
            effect = self._create_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect
        return make_panel_user(user.username)

    async def get_user(self, username: str):
        return make_panel_user(username)


class FakeState:
    """Minimal FSMContext stand-in."""

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


class FakeBot:
    def __init__(self, username: str = "TestBot"):
        self.username = username
        self.deleted: list[tuple[int, int]] = []
        self.sent: list[dict] = []
        self.pinned: list[dict] = []
        self._next_id = 1000

    async def get_me(self):
        return SimpleNamespace(username=self.username)

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted.append((chat_id, message_id))

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def pin_chat_message(self, chat_id: int, message_id: int, **kwargs):
        self.pinned.append({"chat_id": chat_id, "message_id": message_id, **kwargs})


class FakeMessage:
    """Minimal stand-in for an aiogram Message; records replies."""

    def __init__(self, text: str = "", user_id: int = 1):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id, first_name="Owner", username="owner")
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.replies: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies))

    @property
    def texts(self) -> list[str]:
        return [t for t, _ in self.replies]


class FakeChatJoinRequest:
    """Minimal stand-in for an aiogram ChatJoinRequest; records approve/decline calls."""

    def __init__(self, chat_id: int, user_id: int, username: str = "testuser", first_name: str = "Test"):
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id, username=username, first_name=first_name)
        self.date = None
        self.bio = None
        self.invite_link = None
        self.approved = False
        self.declined = False

    async def approve(self):
        self.approved = True
        return True

    async def decline(self):
        self.declined = True
        return True


class FakeBotWithDM(FakeBot):
    """FakeBot that also records DMs sent to users."""

    def __init__(self, username: str = "TestBot"):
        super().__init__(username)
        self.dms: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_id += 1
        self.dms.append((chat_id, text))
        # Also store in sent for compatibility
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=self._next_id)


class FakePanelManager:
    """Minimal PanelManager stand-in that returns FakePanel instances."""

    def __init__(self, panels: dict[int, FakePanel] | None = None):
        self._panels: dict[int, FakePanel] = panels or {}
        self._default = FakePanel()

    def register(self, panel_id: int, panel: FakePanel) -> None:
        self._panels[panel_id] = panel

    def get_client(self, panel) -> FakePanel:
        """Accepts a Panel object or int id."""
        pid = panel.id if hasattr(panel, "id") else int(panel)
        return self._panels.get(pid, self._default)

    async def list_groups(self, panel, *, force: bool = False) -> dict[int, str]:
        """Return groups for a panel as {id: name}."""
        client = self.get_client(panel)
        groups = await client.list_groups_simple()
        return {g.id: g.name for g in groups}

    async def close_all(self) -> None:
        pass
