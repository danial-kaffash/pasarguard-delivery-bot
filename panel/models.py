"""Pydantic models mirroring the PasarGuardAPI schemas we use.

Verified against the live OpenAPI spec (v5.0.3):
  - Token:                              POST /api/admin/token response
  - GroupSimple / GroupsSimpleResponse: GET  /api/groups/simple
  - UserCreate:                         POST /api/user body
  - UserResponse:                       POST /api/user & GET /api/user/{username}
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserStatus(StrEnum):
    """Full user status as returned by the panel."""

    ACTIVE = "active"
    DISABLED = "disabled"
    LIMITED = "limited"
    EXPIRED = "expired"
    ON_HOLD = "on_hold"


class UserStatusCreate(StrEnum):
    """Only these two statuses may be set at creation time."""

    ACTIVE = "active"
    ON_HOLD = "on_hold"


class DataLimitResetStrategy(StrEnum):
    NO_RESET = "no_reset"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class Token(BaseModel):
    """Response of POST /api/admin/token."""

    access_token: str
    token_type: str = "bearer"


class GroupSimple(BaseModel):
    """One entry of GET /api/groups/simple."""

    model_config = ConfigDict(extra="allow")  # tolerate extra panel fields

    id: int
    name: str


class GroupsSimpleResponse(BaseModel):
    groups: list[GroupSimple]
    total: int


class UserCreate(BaseModel):
    """Request body for POST /api/user (UserCreate schema).

    Notes:
      - ``data_limit`` is in **bytes** (5 GB = 5_368_709_120).
      - For on-hold trials set ``status=ON_HOLD``, ``on_hold_expire_duration``
        (seconds of usage once they first connect) and ``on_hold_timeout``
        (absolute deadline for the first connection).
      - ``proxy_settings`` maps protocol -> settings, e.g. ``{"vless": {}}``.
    """

    username: str
    data_limit: int | None = Field(default=None, ge=0, description="bytes")
    data_limit_reset_strategy: DataLimitResetStrategy | None = None
    expire: datetime | int | None = None
    on_hold_expire_duration: int | None = Field(default=None, ge=0, description="seconds")
    on_hold_timeout: datetime | int | None = None
    group_ids: list[int] | None = None
    proxy_settings: dict[str, Any] | None = None
    status: UserStatusCreate | None = None
    note: str | None = Field(default=None, max_length=500)
    auto_delete_in_days: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready dict with unset fields dropped and datetimes serialized."""
        return self.model_dump(mode="json", exclude_none=True)


class UserResponse(BaseModel):
    """Response of POST /api/user and GET /api/user/{username}."""

    model_config = ConfigDict(extra="allow")

    id: int
    username: str
    status: UserStatus
    used_traffic: int
    created_at: datetime
    expire: datetime | int | None = None
    data_limit: int | None = None
    data_limit_reset_strategy: DataLimitResetStrategy | None = None
    on_hold_expire_duration: int | None = None
    on_hold_timeout: datetime | int | None = None
    group_ids: list[int] | None = None
    note: str | None = None
    auto_delete_in_days: int | None = None
    subscription_url: str = ""
    online_at: datetime | None = None
    edit_at: datetime | None = None
