"""Async client for the PasarGuard panel REST API.

Usage::

    async with PasarGuardApiClient(base_url, username, password) as panel:
        groups = await panel.list_groups_simple()
        user = await panel.create_user(UserCreate(username="t123_ab12cd", ...))
        print(user.subscription_url)

Behaviour:
  - Authenticates lazily on the first request (POST /api/admin/token, form login).
  - On a 401 response, re-authenticates **once** and retries the request.
  - Maps HTTP errors onto the exception hierarchy in ``panel.exceptions``.
  - ``verify_ssl=False`` is available for panels with self-signed certificates.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .exceptions import (
    PanelAPIError,
    PanelAuthError,
    PanelConflictError,
    PanelNotFoundError,
    PanelTransportError,
)
from .models import GroupSimple, GroupsSimpleResponse, Token, UserCreate, UserResponse

logger = logging.getLogger(__name__)

TOKEN_PATH = "/api/admin/token"


class PasarGuardApiClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=verify_ssl,
            timeout=timeout,
        )
        self._token: Token | None = None

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> PasarGuardApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth -----------------------------------------------------------------

    async def authenticate(self) -> Token:
        """Log in via POST /api/admin/token and cache the bearer token."""
        try:
            resp = await self._client.post(
                TOKEN_PATH,
                data={
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password,
                },
            )
        except httpx.HTTPError as exc:
            raise PanelTransportError(f"panel login request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise PanelAuthError(
                f"panel login failed with HTTP {resp.status_code}: "
                f"{_extract_detail(resp) or resp.text[:200]}"
            )
        self._token = Token.model_validate(resp.json())
        logger.debug("Authenticated against panel as %r", self._username)
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:  # pragma: no cover — guarded by _request
            raise PanelAuthError("client is not authenticated")
        scheme = self._token.token_type.capitalize()
        return {"Authorization": f"{scheme} {self._token.access_token}"}

    # -- request plumbing ------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        _reauthed: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if self._token is None:
            await self.authenticate()

        try:
            resp = await self._client.request(
                method,
                path,
                headers={**self._auth_headers(), **(headers or {})},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise PanelTransportError(f"request to panel failed: {exc}") from exc

        if resp.status_code == 401 and not _reauthed:
            logger.info("Panel returned 401 — re-authenticating and retrying once.")
            await self.authenticate()
            return await self._request(method, path, headers=headers, _reauthed=True, **kwargs)

        _raise_for_status(resp, method=method, path=path)
        return resp

    # -- endpoints ---------------------------------------------------------------

    async def list_groups_simple(self) -> list[GroupSimple]:
        """GET /api/groups/simple — lightweight list of panel groups."""
        resp = await self._request("GET", "/api/groups/simple")
        return GroupsSimpleResponse.model_validate(resp.json()).groups

    async def create_user(self, user: UserCreate) -> UserResponse:
        """POST /api/user — create a panel user (our 5 GB trial account)."""
        resp = await self._request("POST", "/api/user", json=user.to_payload())
        return UserResponse.model_validate(resp.json())

    async def get_user(self, username: str) -> UserResponse:
        """GET /api/user/{username} — fetch a panel user by username."""
        resp = await self._request("GET", f"/api/user/{username}")
        return UserResponse.model_validate(resp.json())

    async def healthcheck(self) -> bool:
        """GET /health — True when the panel API answers."""
        try:
            resp = await self._client.get("/health")
        except httpx.HTTPError as exc:
            raise PanelTransportError(f"healthcheck failed: {exc}") from exc
        return resp.status_code == 200


def _extract_detail(resp: httpx.Response) -> Any:
    """Best-effort extraction of FastAPI's ``detail`` field from an error body."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        return body.get("detail")
    return body


def _raise_for_status(resp: httpx.Response, *, method: str, path: str) -> None:
    if resp.status_code < 400:
        return
    detail = _extract_detail(resp)
    args = (resp.status_code, detail)
    kwargs = {"method": method, "path": path}
    match resp.status_code:
        case 401:
            raise PanelAuthError(f"panel rejected the token on {method} {path}: {detail}")
        case 404:
            raise PanelNotFoundError(*args, **kwargs)
        case 409:
            raise PanelConflictError(*args, **kwargs)
        case _:
            raise PanelAPIError(*args, **kwargs)
