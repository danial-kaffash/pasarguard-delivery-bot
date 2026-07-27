"""Tests for panel.client.PasarGuardApiClient (HTTP mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from panel.client import PasarGuardApiClient
from panel.exceptions import (
    PanelAuthError,
    PanelConflictError,
    PanelNotFoundError,
    PanelTransportError,
)
from panel.models import (
    DataLimitResetStrategy,
    UserCreate,
    UserStatus,
    UserStatusCreate,
)

BASE = "https://panel.test:8000"
TOKEN_JSON = {"access_token": "tok-1", "token_type": "bearer"}
FIVE_GB = 5 * 1024**3

USER_JSON = {
    "id": 77,
    "username": "t12345_ab12cd",
    "status": "on_hold",
    "used_traffic": 0,
    "created_at": "2026-07-27T10:00:00Z",
    "data_limit": FIVE_GB,
    "group_ids": [2, 5],
    "subscription_url": f"{BASE}/sub/abc123/",
}


def make_client() -> PasarGuardApiClient:
    return PasarGuardApiClient(BASE, "greet-bot", "secret", verify_ssl=False)


async def test_authenticate_sends_form_login():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        route = router.post("/api/admin/token").mock(
            return_value=httpx.Response(200, json=TOKEN_JSON)
        )
        async with make_client() as panel:
            token = await panel.authenticate()

    assert token.access_token == "tok-1"
    request = route.calls.last.request
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    body = request.content.decode()
    assert "grant_type=password" in body
    assert "username=greet-bot" in body
    assert "password=secret" in body


async def test_auth_failure_raises_panel_auth_error():
    async with respx.mock(base_url=BASE) as router:
        router.post("/api/admin/token").mock(
            return_value=httpx.Response(401, json={"detail": "Incorrect username or password"})
        )
        async with make_client() as panel:
            with pytest.raises(PanelAuthError):
                await panel.authenticate()


async def test_list_groups_simple():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        route = router.get("/api/groups/simple").mock(
            return_value=httpx.Response(
                200,
                json={
                    "groups": [
                        {"id": 2, "name": "NL-Amazon-TCP"},
                        {"id": 5, "name": "TR-Istanbul"},
                    ],
                    "total": 2,
                },
            )
        )
        async with make_client() as panel:
            groups = await panel.list_groups_simple()

    assert [(g.id, g.name) for g in groups] == [(2, "NL-Amazon-TCP"), (5, "TR-Istanbul")]
    # the bearer token must be attached
    assert route.calls.last.request.headers["authorization"] == "Bearer tok-1"


async def test_create_user_posts_expected_payload():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        route = router.post("/api/user").mock(return_value=httpx.Response(200, json=USER_JSON))

        user_in = UserCreate(
            username="t12345_ab12cd",
            data_limit=FIVE_GB,
            data_limit_reset_strategy=DataLimitResetStrategy.NO_RESET,
            status=UserStatusCreate.ON_HOLD,
            on_hold_expire_duration=259200,
            group_ids=[2, 5],
            proxy_settings={"vless": {}},
            auto_delete_in_days=11,
        )
        async with make_client() as panel:
            user = await panel.create_user(user_in)

    sent = route.calls.last.request
    import json

    payload = json.loads(sent.content)
    assert payload["username"] == "t12345_ab12cd"
    assert payload["data_limit"] == FIVE_GB
    assert payload["status"] == "on_hold"
    assert payload["on_hold_expire_duration"] == 259200
    assert payload["group_ids"] == [2, 5]
    assert "expire" not in payload  # None fields are dropped

    assert user.status is UserStatus.ON_HOLD
    assert user.subscription_url.endswith("/sub/abc123/")


async def test_get_user():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        router.get("/api/user/t12345_ab12cd").mock(return_value=httpx.Response(200, json=USER_JSON))
        async with make_client() as panel:
            user = await panel.get_user("t12345_ab12cd")

    assert user.username == "t12345_ab12cd"


async def test_auto_reauth_on_401_then_success():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        token_route = router.post("/api/admin/token").mock(
            return_value=httpx.Response(200, json=TOKEN_JSON)
        )
        router.get("/api/user/u1").mock(
            side_effect=[
                httpx.Response(401, json={"detail": "Not authenticated"}),
                httpx.Response(200, json={**USER_JSON, "username": "u1"}),
            ]
        )
        async with make_client() as panel:
            user = await panel.get_user("u1")

    assert user.username == "u1"
    assert token_route.call_count == 2  # initial auth + re-auth after 401


async def test_401_twice_raises_auth_error():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        router.get("/api/user/u1").mock(
            return_value=httpx.Response(401, json={"detail": "Not authenticated"})
        )
        async with make_client() as panel:
            with pytest.raises(PanelAuthError):
                await panel.get_user("u1")


async def test_409_raises_conflict():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        router.post("/api/user").mock(
            return_value=httpx.Response(409, json={"detail": "User already exists"})
        )
        async with make_client() as panel:
            with pytest.raises(PanelConflictError) as exc_info:
                await panel.create_user(UserCreate(username="dup"))

    assert exc_info.value.status_code == 409
    assert "already exists" in str(exc_info.value.detail)


async def test_404_raises_not_found():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        router.get("/api/user/ghost").mock(
            return_value=httpx.Response(404, json={"detail": "User not found"})
        )
        async with make_client() as panel:
            with pytest.raises(PanelNotFoundError):
                await panel.get_user("ghost")


async def test_transport_error_is_wrapped():
    async with respx.mock(base_url=BASE, assert_all_called=True) as router:
        router.post("/api/admin/token").mock(return_value=httpx.Response(200, json=TOKEN_JSON))
        router.get("/api/groups/simple").mock(side_effect=httpx.ConnectError("boom"))
        async with make_client() as panel:
            with pytest.raises(PanelTransportError):
                await panel.list_groups_simple()
