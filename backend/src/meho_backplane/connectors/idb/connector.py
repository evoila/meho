# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``idb`` connectors: Workspace ONE Access 3.3 and VCF Identity Broker 9.x.

Both products are single sign-on services for VMware products, but their APIs
share almost no paths (see ``docs/decisions/2026-10-23-identity-broker-connector.md``).
So there are two implementations of one product ``idb``:

* :class:`IdbWsaConnector` -- ``idb-wsa-3.3``, Workspace ONE Access 3.3
  (band ``>=3.3,<4.0``).
* :class:`IdbRestConnector` -- ``idb-rest-9.0``, VCF Identity Broker 9.x
  (band ``>=9.0,<10.0``).

Both register the same six operation ids, so the agent calls
``idb.user.find`` on either product and MEHO runs the right call.

Login
-----

Both log in with an OAuth 2.0 client: ``POST`` to the token path with HTTP
Basic (``client_id:client_secret``) and the form body
``grant_type=client_credentials``. The answer's ``access_token`` goes out as
``Authorization: Bearer <token>``. The token is cached per credential and
renewed shortly before ``expires_in`` runs out. When a call gets ``401``, the
dispatcher calls :meth:`invalidate_session` with the refused request; the
token is dropped only when that request sent it, and the dispatcher sends the
call once more with a new login. A refused login (``400``, ``401``, ``403``)
becomes ``connector_auth_failed`` and forgets the cached secret, so a
corrected secret works without a restart.

The login call is sent without a trace, so its token is never recorded.
:meth:`trace_response_body` masks token and password fields in the answers
that are recorded.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import credential_cache_key
from meho_backplane.connectors._shared.rejected_token import drop_rejected_token
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    basic_auth_header,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.idb import _paths as paths
from meho_backplane.connectors.idb.session import (
    IdbCredentials,
    IdbCredentialsLoader,
    IdbTargetLike,
    load_credentials_from_vault,
    resolve_tenant,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["IdbConnectorBase", "IdbRestConnector", "IdbWsaConnector"]

_log = structlog.get_logger(__name__)

#: Field names whose values never reach a stored trace or a result.
SECRET_FIELDS: Final[frozenset[str]] = frozenset(
    {"access_token", "refresh_token", "id_token", "token", "password", "client_secret", "secret"}
)
_MASK: Final[str] = "<redacted>"

#: Renew the token this many seconds before it runs out.
_TOKEN_MARGIN_S: Final[float] = 30.0
#: Token life when the answer has no ``expires_in``.
_DEFAULT_TOKEN_LIFE_S: Final[float] = 300.0

AUTH_REMEDIATION: Final[str] = (
    "Check the secret of the target: 'client_id' and 'client_secret' must be an OAuth client "
    "with the client-credentials grant and admin rights. For Identity Broker, also check "
    "extras.tenant (default CUSTOMER)."
)

_CacheKey = tuple[str, ...]


def mask_secrets(value: Any) -> Any:
    """Return *value* with every token, password and secret field masked."""
    if isinstance(value, dict):
        return {
            key: (_MASK if str(key).lower() in SECRET_FIELDS else mask_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


@dataclass(frozen=True)
class _Token:
    value: str = field(repr=False)
    expires_at: float

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.value}"}


def _may_use_cache(operator: Operator) -> bool:
    """Only a call with a person's token may use a cached secret or token."""
    return bool(operator.raw_jwt) and not is_system_operator(operator)


class IdbConnectorBase(HttpConnector):
    """Shared login, cache, call helpers and fingerprint of both ``idb`` connectors."""

    product = "idb"
    version = ""
    impl_id = ""
    supported_version_range: str | None = None
    priority = 1

    #: The token path of the product (a template from :mod:`._paths`).
    token_path: str = ""

    def __init__(self, *, credentials_loader: IdbCredentialsLoader | None = None) -> None:
        super().__init__()
        self._credentials_loader: IdbCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._credentials: dict[_CacheKey, IdbCredentials] = {}
        self._tokens: dict[_CacheKey, _Token] = {}
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def path_values(self, target: IdbTargetLike) -> dict[str, object]:
        """The values every path of this product needs (the Identity Broker tenant)."""
        del target
        return {}

    def path(
        self, target: IdbTargetLike, template: str, values: dict[str, object] | None = None
    ) -> str:
        return paths.fill(template, {**self.path_values(target), **(values or {})})

    # ------------------------------------------------------------------
    # Login and cache
    # ------------------------------------------------------------------

    def _key(self, target: IdbTargetLike) -> _CacheKey:
        return credential_cache_key(target, *self.path_values(target).values())

    async def auth_headers(self, target: IdbTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}``, logging in when needed."""
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"{type(self).__name__} only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target {target.name!r} "
                f"asked for auth_model={auth_model!r}"
            )
        key = self._key(target)
        use_cache = _may_use_cache(operator)
        async with self._token_lock:
            token = self._tokens.get(key) if use_cache else None
            if token is not None and token.expires_at > time.monotonic():
                return token.headers()
            credentials = self._credentials.get(key) if use_cache else None
            if credentials is None:
                credentials = await self._credentials_loader(target, operator)
                self._credentials[key] = credentials
            try:
                token = await self._login(target, credentials)
            except Exception:
                # The secret may be wrong: the next call must read it again.
                self._credentials.pop(key, None)
                raise
            self._tokens[key] = token
            return token.headers()

    async def _login(self, target: IdbTargetLike, credentials: IdbCredentials) -> _Token:
        """Get a token with the client-credentials grant. No trace is recorded."""
        path = self.path(target, self.token_path)
        client = await self._http_client(target)
        response = await client.post(
            path,
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": basic_auth_header(
                    credentials.client_id, credentials.client_secret
                ),
                "Accept": "application/json",
            },
            extensions=self._request_extensions(target),
        )
        if response.status_code in (400, 401, 403):
            raise ConnectorAuthError(
                f"the OAuth login for target {target.name!r} was refused with HTTP "
                f"{response.status_code} (POST {self.token_path})",
                status_code=response.status_code,
                cause=f"idb_login_{response.status_code}",
                target_name=target.name,
                host=target.host,
                secret_ref=target.secret_ref,
                remediation=AUTH_REMEDIATION,
            )
        response.raise_for_status()
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        value = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            raise RuntimeError(
                f"the OAuth login for target {target.name!r} returned no access_token"
            )
        life = body.get("expires_in")
        seconds = float(life) if isinstance(life, int | float) and life > 0 else None
        expires_at = time.monotonic() + max(
            (seconds or _DEFAULT_TOKEN_LIFE_S) - _TOKEN_MARGIN_S, 1.0
        )
        _log.info("idb_login_done", target=target.name, impl_id=self.impl_id)
        return _Token(value=value, expires_at=expires_at)

    async def invalidate_session(
        self, target: IdbTargetLike, *, rejected: httpx.Request | None = None
    ) -> None:
        """Drop the cached token of *target* (the dispatcher's 401 hook).

        With *rejected*, the token is dropped only when that request sent it,
        so a late ``401`` never drops a newer token.
        """
        async with self._token_lock:
            drop_rejected_token(self._tokens, self._key(target), rejected, _Token.headers)

    async def invalidate_credentials(self, target: IdbTargetLike) -> None:
        """Forget the cached secret and token of *target* (after a refused login)."""
        key = self._key(target)
        async with self._token_lock:
            self._credentials.pop(key, None)
            self._tokens.pop(key, None)

    async def aclose(self) -> None:
        """Forget every cached secret and token, then close the HTTP clients."""
        self._credentials.clear()
        self._tokens.clear()
        await super().aclose()

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Record answers with every token, password and secret field masked."""
        try:
            body = response.json()
        except ValueError:
            return None
        try:
            return mask_secrets(body)
        except Exception:  # a trace must never break the call, and never leak
            return _MASK

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    async def api_get(
        self,
        target: IdbTargetLike,
        operator: Operator,
        template: str,
        values: dict[str, object] | None = None,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> dict[str, Any]:
        """``GET`` one path and return the JSON answer. HTTP errors raise unchanged."""
        return await self._request_json(
            target,
            "GET",
            self.path(target, template, values),
            operator=operator,
            params=params,
            extra_headers={"Accept": accept},
        )

    async def api_post(
        self,
        target: IdbTargetLike,
        operator: Operator,
        template: str,
        values: dict[str, object] | None = None,
        *,
        json: dict[str, Any],
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        """``POST`` one JSON body and return the JSON answer. Never sent twice."""
        return await self._post_json(
            target,
            self.path(target, template, values),
            operator=operator,
            json=json,
            extra_headers={"Content-Type": content_type, "Accept": content_type},
        )

    # ------------------------------------------------------------------
    # Fingerprint and probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: IdbTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Tell which product answers, and its version when it says (see :mod:`._fingerprint`)."""
        from meho_backplane.connectors.idb._fingerprint import fingerprint_target

        return await fingerprint_target(self, target, operator)

    async def probe(self, target: IdbTargetLike) -> ProbeResult:
        """Check the target with the same calls as :meth:`fingerprint`."""
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False, reason=str(fp.extras.get("error", "unreachable")), probed_at=fp.probed_at
        )

    async def execute(
        self,
        target: IdbTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Old entry point. Sends the call through the dispatcher.

        This path runs as MEHO's system identity, which cannot read the
        target's secret, so a call through it fails on purpose.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub=f"system:{self.impl_id}-connector-shim",
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


class IdbWsaConnector(IdbConnectorBase):
    """Workspace ONE Access 3.3: ``("idb", "3.3", "idb-wsa")``, connector ``idb-wsa-3.3``."""

    version = "3.3"
    impl_id = "idb-wsa"
    supported_version_range = ">=3.3,<4.0"
    token_path = paths.WSA_TOKEN_PATH

    async def about(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.about`` on Workspace ONE Access."""
        from meho_backplane.connectors.idb.reads import wsa_about

        return await wsa_about(self, operator, target, params)

    async def identity_provider_list(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.identity_provider.list`` on Workspace ONE Access."""
        from meho_backplane.connectors.idb.reads import wsa_identity_provider_list

        return await wsa_identity_provider_list(self, operator, target, params)

    async def directory_list(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.directory.list`` on Workspace ONE Access."""
        from meho_backplane.connectors.idb.reads import wsa_directory_list

        return await wsa_directory_list(self, operator, target, params)

    async def directory_sync(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.directory.sync`` on Workspace ONE Access (waits for approval)."""
        from meho_backplane.connectors.idb.reads import wsa_directory_sync

        return await wsa_directory_sync(self, operator, target, params)

    async def user_find(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.user.find`` on Workspace ONE Access."""
        from meho_backplane.connectors.idb.reads import scim_user_find

        return await scim_user_find(self, operator, target, params, paths.WSA_USERS_SEARCH_PATH)

    async def group_find(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.group.find`` on Workspace ONE Access."""
        from meho_backplane.connectors.idb.reads import scim_group_find

        return await scim_group_find(self, operator, target, params, paths.WSA_GROUPS_SEARCH_PATH)


class IdbRestConnector(IdbConnectorBase):
    """VCF Identity Broker 9.x: ``("idb", "9.0", "idb-rest")``, connector ``idb-rest-9.0``.

    The Identity Broker API has no call for identity providers, directories or
    a directory sync. On VCF 9.1 those calls are in the VCF Operations API, so
    the three operations stop early here with a message that says so.
    """

    version = "9.0"
    impl_id = "idb-rest"
    supported_version_range = ">=9.0,<10.0"
    token_path = paths.IDB_TOKEN_PATH

    def path_values(self, target: IdbTargetLike) -> dict[str, object]:
        return {"tenant": resolve_tenant(target)}

    async def about(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.about`` on Identity Broker."""
        from meho_backplane.connectors.idb.reads import rest_about

        return await rest_about(self, operator, target, params)

    async def identity_provider_list(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.identity_provider.list``: not in the Identity Broker API."""
        from meho_backplane.connectors.idb.reads import not_on_identity_broker

        not_on_identity_broker("idb.identity_provider.list", target)
        return {}

    async def directory_list(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.directory.list``: not in the Identity Broker API."""
        from meho_backplane.connectors.idb.reads import not_on_identity_broker

        not_on_identity_broker("idb.directory.list", target)
        return {}

    async def directory_sync(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.directory.sync``: not in the Identity Broker API."""
        from meho_backplane.connectors.idb.reads import not_on_identity_broker

        not_on_identity_broker("idb.directory.sync", target)
        return {}

    async def user_find(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.user.find`` on Identity Broker."""
        from meho_backplane.connectors.idb.reads import scim_user_find

        return await scim_user_find(self, operator, target, params, paths.IDB_USERS_SEARCH_PATH)

    async def group_find(
        self, operator: Operator, target: IdbTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``idb.group.find`` on Identity Broker."""
        from meho_backplane.connectors.idb.reads import scim_group_find

        return await scim_group_find(self, operator, target, params, paths.IDB_GROUPS_SEARCH_PATH)
