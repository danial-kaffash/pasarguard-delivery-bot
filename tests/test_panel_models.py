"""Tests for panel.models — serialization towards the API contract."""

from __future__ import annotations

from datetime import UTC, datetime

from panel.models import (
    DataLimitResetStrategy,
    UserCreate,
    UserResponse,
    UserStatus,
    UserStatusCreate,
)

FIVE_GB = 5 * 1024**3


def test_user_create_on_hold_payload():
    timeout = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    user = UserCreate(
        username="t12345_ab12cd",
        data_limit=FIVE_GB,
        data_limit_reset_strategy=DataLimitResetStrategy.NO_RESET,
        status=UserStatusCreate.ON_HOLD,
        on_hold_expire_duration=3 * 86400,
        on_hold_timeout=timeout,
        group_ids=[2, 5],
        proxy_settings={"vless": {}},
        note="telegram-greet-bot tg_id=12345",
        auto_delete_in_days=11,
    )
    payload = user.to_payload()

    assert payload["username"] == "t12345_ab12cd"
    assert payload["data_limit"] == FIVE_GB
    assert payload["data_limit_reset_strategy"] == "no_reset"
    assert payload["status"] == "on_hold"
    assert payload["on_hold_expire_duration"] == 259200
    # datetime must be serialized to an ISO string, not left as a datetime
    assert payload["on_hold_timeout"] == "2026-08-03T12:00:00Z"
    assert payload["group_ids"] == [2, 5]
    assert payload["proxy_settings"] == {"vless": {}}
    assert payload["auto_delete_in_days"] == 11


def test_user_create_drops_none_fields():
    payload = UserCreate(username="minimal").to_payload()
    assert payload == {"username": "minimal"}


def test_user_create_rejects_negative_limit():
    import pydantic

    try:
        UserCreate(username="x", data_limit=-1)
        raised = False
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_user_response_parses_panel_shape():
    data = {
        "id": 77,
        "username": "t12345_ab12cd",
        "status": "on_hold",
        "used_traffic": 0,
        "created_at": "2026-07-27T10:00:00Z",
        "data_limit": FIVE_GB,
        "group_ids": [2, 5],
        "subscription_url": "https://panel.test:8000/sub/abc123/",
        "lifetime_used_traffic": 0,
        "some_future_field": True,  # extra fields must not break parsing
    }
    user = UserResponse.model_validate(data)
    assert user.id == 77
    assert user.status is UserStatus.ON_HOLD
    assert user.subscription_url.endswith("/sub/abc123/")
    assert user.group_ids == [2, 5]
