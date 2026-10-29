# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PhpipamConnector -- the typed connector for phpIPAM 1.x (IP address management).

One target is one phpIPAM API app. The connector reads subnets and free
addresses, and reserves or releases one address. The operation bodies live
in :mod:`.reads` and :mod:`.writes`; this module holds the login, the one
helper every vendor call goes through (:meth:`PhpipamConnector.api_call`),
the fingerprint, the probe and the thin handler methods the dispatcher binds.

Login
-----

The secret (see :mod:`.session`) is either the app code of the API app or a
phpIPAM user. With an app code, MEHO sends it as the ``phpipam-token`` header.
With a user, MEHO logs in once with ``POST /api/{app_id}/user/`` (HTTP Basic),
keeps the token it gets, and sends that. When phpIPAM refuses the token later
(401 or 403; a user token runs out after some hours), MEHO logs in again once
and sends the call again. phpIPAM checks the token before it runs a call, so
a refused call changed nothing and sending it again is safe. A second refusal
becomes ``connector_auth_failed``.

The login call is sent without a trace, so the token in its answer is never
recorded. :meth:`PhpipamConnector.trace_response_body` also masks every
``token`` or ``password`` field in the answers that are recorded.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import is_system_operator
from meho_backplane.connectors._shared.vcf_auth import (
    basic_auth_header,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.phpipam._errors import (
    PhpipamApiError,
    auth_error,
    phpipam_message,
)
from meho_backplane.connectors.phpipam._paths import _USER_PATH, api_path
from meho_backplane.connectors.phpipam.session import (
    PhpipamCredentials,
    PhpipamCredentialsLoader,
    PhpipamTargetLike,
    load_credentials_from_vault,
    resolve_target_config,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["PhpipamConnector"]

_log = structlog.get_logger(__name__)

#: The header phpIPAM reads the token from (``token`` works too).
TOKEN_HEADER: Final[str] = "phpipam-token"

#: Field names whose values never reach a stored trace.
_SECRET_FIELDS: Final[frozenset[str]] = frozenset({"token", "password", "app_code"})
_MASK: Final[str] = "<redacted>"

_PROBE_METHOD: Final[str] = "GET /api/{app_id}/user/ (no token)"

_CacheKey = tuple[str, ...]


def _may_use_cache(operator: Operator) -> bool:
    """Only a call with a person's token may use a cached secret or token."""
    return bool(operator.raw_jwt) and not is_system_operator(operator)


def _mask_secrets(value: Any) -> Any:
    """Return *value* with every ``token`` / ``password`` field masked."""
    if isinstance(value, dict):
        return {
            key: (_MASK if str(key).lower() in _SECRET_FIELDS else _mask_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


class PhpipamConnector(HttpConnector):
    """phpIPAM 1.x REST connector.

    Registered as ``("phpipam", "1.x", "phpipam-rest")``, so the connector ID
    is ``phpipam-rest-1.x``, plus the wildcard row ``("phpipam", "", "")``.
    phpIPAM's API reports no product version, so ``supported_version_range``
    stays ``None``.
    """

    product = "phpipam"
    version = "1.x"
    impl_id = "phpipam-rest"
    supported_version_range: str | None = None
    priority = 1

    def __init__(self, *, credentials_loader: PhpipamCredentialsLoader | None = None) -> None:
        super().__init__()
        self._credentials_loader: PhpipamCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._credentials: dict[_CacheKey, PhpipamCredentials] = {}
        self._tokens: dict[_CacheKey, str] = {}
        self._locks: dict[_CacheKey, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Login and cache
    # ------------------------------------------------------------------

    @staticmethod
    def _key(target: PhpipamTargetLike) -> _CacheKey:
        return (*target_cache_key(target), str(target.secret_ref or ""))

    async def auth_headers(self, target: PhpipamTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"phpipam-token": <token>}`` for a call to *target*."""
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"PhpipamConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target {target.name!r} "
                f"asked for auth_model={auth_model!r}"
            )
        key = self._key(target)
        use_cache = _may_use_cache(operator)
        async with self._locks.setdefault(key, asyncio.Lock()):
            credentials = self._credentials.get(key) if use_cache else None
            if credentials is None:
                credentials = await self._credentials_loader(target, operator)
                self._credentials[key] = credentials
            if credentials.app_token:
                return {TOKEN_HEADER: credentials.app_token}
            token = self._tokens.get(key) if use_cache else None
            if token is None:
                token = await self._login(target, credentials)
                self._tokens[key] = token
            return {TOKEN_HEADER: token}

    async def _login(self, target: PhpipamTargetLike, credentials: PhpipamCredentials) -> str:
        """Log in as the phpIPAM user and return the token. No trace is recorded."""
        config = resolve_target_config(target)
        path = api_path(config.base_path, _USER_PATH, {"app_id": config.app_id})
        client = await self._http_client(target)
        response = await client.post(
            path,
            headers={
                "Authorization": basic_auth_header(
                    credentials.username or "", credentials.password or ""
                )
            },
            extensions=self._request_extensions(target),
        )
        if response.status_code in (401, 403):
            raise auth_error(response, target=target, stage="login")
        if response.status_code >= 400:
            raise PhpipamApiError(
                f"phpIPAM user login for target {target.name!r} failed with HTTP "
                f"{response.status_code}",
                http_status=response.status_code,
                upstream_message=phpipam_message(response),
            )
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        data = body.get("data") if isinstance(body, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise PhpipamApiError(
                f"phpIPAM user login for target {target.name!r} returned no token",
                http_status=response.status_code,
                upstream_message=None,
            )
        _log.info("phpipam_login_done", target=target.name, app_id=config.app_id)
        return token

    def _drop_token(self, target: PhpipamTargetLike) -> None:
        self._tokens.pop(self._key(target), None)

    async def invalidate_credentials(self, target: PhpipamTargetLike) -> None:
        """Forget the target's cached secret and token (after a refused login).

        The next call reads the secret again, so a corrected secret works
        without a restart.
        """
        prefix = target_cache_key(target)
        for cache in (self._credentials, self._tokens):
            for key in [key for key in cache if key[:2] == prefix]:
                del cache[key]

    async def aclose(self) -> None:
        """Forget every cached secret and token, then close the HTTP clients."""
        self._credentials.clear()
        self._tokens.clear()
        await super().aclose()

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Record phpIPAM answers with every ``token`` / ``password`` field masked."""
        try:
            body = response.json()
        except ValueError:
            return None
        try:
            return _mask_secrets(body)
        except Exception:  # a trace must never break the call, and never leak
            return _MASK

    # ------------------------------------------------------------------
    # The one way to call phpIPAM
    # ------------------------------------------------------------------

    async def api_call(
        self,
        target: PhpipamTargetLike,
        operator: Operator,
        method: str,
        template: str,
        values: dict[str, object] | None = None,
        *,
        json: dict[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> dict[str, Any] | None:
        """Send one call to phpIPAM and return its answer (``{code, success, data, ...}``).

        ``None`` when phpIPAM answers 404 and *not_found_ok* is set (phpIPAM
        answers 404 for "no addresses", "no subnets" and "address not
        reserved"). A 401 or 403 with a user token logs in again and sends the
        call once more. Every other error answer raises
        :class:`~meho_backplane.connectors.phpipam._errors.PhpipamApiError`.
        A lost answer (time-out, broken connection) raises
        :class:`httpx.TransportError` unchanged: a write is never sent twice.
        """
        config = resolve_target_config(target)
        path = api_path(config.base_path, template, {"app_id": config.app_id, **(values or {})})
        for attempt in (1, 2):
            try:
                if method == "GET":
                    body = await self._get_json(target, path, operator=operator)
                else:
                    body = await self._post_json(
                        target,
                        path,
                        operator=operator,
                        verb=method,
                        json=json,
                    )
            except httpx.HTTPStatusError as exc:
                response = exc.response
                status = response.status_code
                if status in (401, 403):
                    key = self._key(target)
                    credentials = self._credentials.get(key)
                    if attempt == 1 and credentials is not None and not credentials.app_token:
                        self._drop_token(target)
                        continue
                    raise auth_error(
                        response, target=target, stage="call", after_relogin=attempt == 2
                    ) from exc
                if status == 404 and not_found_ok:
                    return None
                raise PhpipamApiError(
                    f"phpIPAM answered HTTP {status} to {method} {template}",
                    http_status=status,
                    upstream_message=phpipam_message(response),
                ) from exc
            return body if isinstance(body, dict) else {}
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------
    # Fingerprint and probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: PhpipamTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Check that the phpIPAM API answers for this app id. Sends no token.

        ``GET /api/{app_id}/user/`` without a token gets a phpIPAM answer
        (``{code, success, message}``, usually 401 or 403) from every phpIPAM
        1.x. Such an answer means the API is reachable; ``extras.api_code``
        and ``extras.api_message`` say what phpIPAM answered (for example
        "Invalid application id" for a wrong ``extras.app_id``). phpIPAM
        reports no version here, so ``version`` stays ``None``.
        """
        del operator
        probed_at = datetime.now(UTC)
        try:
            config = resolve_target_config(target)
            path = api_path(config.base_path, _USER_PATH, {"app_id": config.app_id})
            client = await self._http_client(target)
            response = await client.get(
                path,
                extensions=self._request_extensions(target),
            )
            body: Any = response.json()
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="phpipam",
                product="phpipam",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        if not (isinstance(body, dict) and "code" in body and "success" in body):
            return FingerprintResult(
                vendor="phpipam",
                product="phpipam",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": "the answer is not a phpIPAM API answer"},
            )
        return FingerprintResult(
            vendor="phpipam",
            product="phpipam",
            version=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={
                "app_id": config.app_id,
                "api_code": body.get("code"),
                "api_message": phpipam_message(response),
            },
        )

    async def probe(self, target: PhpipamTargetLike) -> ProbeResult:
        """Check the target with the same call as :meth:`fingerprint`."""
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False, reason=str(fp.extras.get("error", "unreachable")), probed_at=fp.probed_at
        )

    async def execute(
        self,
        target: PhpipamTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Old entry point. Sends the call through the dispatcher.

        This path runs as MEHO's system identity, which cannot read the
        target's secret, so a phpIPAM call through it fails on purpose.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:phpipam-rest-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        return await dispatch(
            operator=operator,
            connector_id=f"{self.impl_id}-{self.version}",
            op_id=op_id,
            target=target,
            params=params,
        )

    # ------------------------------------------------------------------
    # Typed operations (bodies in reads.py and writes.py)
    # ------------------------------------------------------------------

    async def subnet_list(
        self, operator: Operator, target: PhpipamTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``phpipam.subnet.list``: subnets and how full each is."""
        from meho_backplane.connectors.phpipam.reads import subnet_list_impl

        return await subnet_list_impl(self, operator, target, params)

    async def subnet_free_addresses(
        self, operator: Operator, target: PhpipamTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``phpipam.subnet.free_addresses``: free addresses of one subnet."""
        from meho_backplane.connectors.phpipam.reads import free_addresses_impl

        return await free_addresses_impl(self, operator, target, params)

    async def address_reserve(
        self, operator: Operator, target: PhpipamTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``phpipam.address.reserve``: reserve one address (waits for approval)."""
        from meho_backplane.connectors.phpipam.writes import reserve_impl

        return await reserve_impl(self, operator, target, params)

    async def address_release(
        self, operator: Operator, target: PhpipamTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``phpipam.address.release``: release one address (waits for approval)."""
        from meho_backplane.connectors.phpipam.writes import release_impl

        return await release_impl(self, operator, target, params)
