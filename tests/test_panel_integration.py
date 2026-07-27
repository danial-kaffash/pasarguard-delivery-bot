"""Integration tests: the real PasarGuardApiClient over an actual HTTP socket,
against a tiny local mock of the panel (stdlib http.server)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from panel.client import PasarGuardApiClient
from panel.exceptions import PanelNotFoundError
from panel.models import DataLimitResetStrategy, UserCreate, UserStatusCreate

FIVE_GB = 5 * 1024**3


class _PanelState:
    def __init__(self):
        self.users: dict[str, dict] = {}
        self.valid_tokens: set[str] = set()
        self.token_counter = 0

    def issue_token(self) -> str:
        self.token_counter += 1
        token = f"tok-{self.token_counter}"
        self.valid_tokens.add(token)
        return token


def _make_handler(state: _PanelState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep test output clean
            pass

        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth.startswith("Bearer ") and auth[7:] in state.valid_tokens

        def _user_response(self, username: str) -> dict:
            return {
                "id": 500 + len(state.users),
                "username": username,
                "status": "on_hold",
                "used_traffic": 0,
                "created_at": datetime.now(UTC).isoformat(),
                "subscription_url": f"http://panel.local/sub/{username}/",
            }

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))

            if self.path == "/api/admin/token":
                form = parse_qs(raw.decode())
                if (
                    form.get("username", [""])[0] == "admin"
                    and form.get("password", [""])[0] == "secret"
                ):
                    self._send(200, {"access_token": state.issue_token(), "token_type": "bearer"})
                else:
                    self._send(401, {"detail": "Incorrect username or password"})
                return

            if not self._authorized():
                self._send(401, {"detail": "Not authenticated"})
                return

            if self.path == "/api/user":
                data = json.loads(raw)
                if data["username"] in state.users:
                    self._send(409, {"detail": "User already exists"})
                    return
                state.users[data["username"]] = data
                self._send(200, self._user_response(data["username"]))
                return

            self._send(404, {"detail": "not found"})

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
                return
            if not self._authorized():
                self._send(401, {"detail": "Not authenticated"})
                return
            if self.path == "/api/groups/simple":
                self._send(
                    200,
                    {
                        "groups": [{"id": 2, "name": "NL"}, {"id": 5, "name": "TR"}],
                        "total": 2,
                    },
                )
                return
            if self.path.startswith("/api/user/"):
                username = self.path.rsplit("/", 1)[1]
                if username in state.users:
                    self._send(200, self._user_response(username))
                else:
                    self._send(404, {"detail": "User not found"})
                return
            self._send(404, {"detail": "not found"})

    return Handler


@pytest.fixture
def mock_panel_server():
    state = _PanelState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_full_flow_over_real_http(mock_panel_server):
    state, base_url = mock_panel_server
    async with PasarGuardApiClient(base_url, "admin", "secret") as panel:
        assert await panel.healthcheck() is True

        groups = await panel.list_groups_simple()
        assert {g.id for g in groups} == {2, 5}

        user_in = UserCreate(
            username="t777_a1b2c3",
            data_limit=FIVE_GB,
            data_limit_reset_strategy=DataLimitResetStrategy.NO_RESET,
            status=UserStatusCreate.ON_HOLD,
            on_hold_expire_duration=259200,
            group_ids=[2, 5],
            proxy_settings={"vless": {}},
        )
        created = await panel.create_user(user_in)
        assert created.subscription_url.endswith("/sub/t777_a1b2c3/")

        # what actually arrived at the server (proves wire serialization)
        received = state.users["t777_a1b2c3"]
        assert received["data_limit"] == FIVE_GB
        assert received["status"] == "on_hold"
        assert received["group_ids"] == [2, 5]
        assert received["proxy_settings"] == {"vless": {}}

        fetched = await panel.get_user("t777_a1b2c3")
        assert fetched.username == "t777_a1b2c3"


async def test_auto_reauth_over_real_http(mock_panel_server):
    state, base_url = mock_panel_server
    async with PasarGuardApiClient(base_url, "admin", "secret") as panel:
        groups = await panel.list_groups_simple()  # authenticates once
        assert groups

        # invalidate every issued token — next call must hit 401, re-login, retry
        state.valid_tokens.clear()
        groups_again = await panel.list_groups_simple()
        assert {g.id for g in groups_again} == {2, 5}
        assert state.token_counter == 2  # exactly one re-auth happened


async def test_404_over_real_http(mock_panel_server):
    _state, base_url = mock_panel_server
    async with PasarGuardApiClient(base_url, "admin", "secret") as panel:
        with pytest.raises(PanelNotFoundError):
            await panel.get_user("ghost")
