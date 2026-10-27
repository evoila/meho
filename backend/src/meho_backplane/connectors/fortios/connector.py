# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FortiOSConnector -- hand-written connector for the FortiGate REST API (FortiOS 7.2 to 7.x).

Login
-----

An API token of a FortiOS REST API administrator, read from the target's
Vault secret (:func:`~meho_backplane.connectors.fortios.target.load_token_from_vault`)
and sent as ``Authorization: Bearer <token>``. No password login, no cookies.
A 401 stops the call with "token refused" and is never retried: the
connector has no ``invalidate_session`` hook, so the dispatcher does not
send the call again, and the shared HTTP base retries only 5xx answers and
connection errors.

TLS
---

Checks stay on. The operator pins the device's certificate or CA
(``tls_ca_pin``) and sets ``tls_server_name`` when the host is an IP
address; both come from the shared HTTP base.

Calls
-----

:meth:`FortiOSConnector.api_get` and :meth:`FortiOSConnector.api_write` add
the target's VDOM to every call (see
:mod:`~meho_backplane.connectors.fortios.target`), check the answer wrapper
(:mod:`~meho_backplane.connectors.fortios._answer`) and remove secrets from
the answer before anything else sees it. The stored trace of a call is
cleaned the same way (:meth:`FortiOSConnector.trace_response_body`).
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError, is_acceptable_auth_model
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.fortios import reads
from meho_backplane.connectors.fortios._answer import FortiOSAnswer, unwrap_answer
from meho_backplane.connectors.fortios._errors import (
    FortiOSApiError,
    FortiOSScopeError,
    FortiOSTargetError,
    FortiOSVersionError,
    error_from_status,
)
from meho_backplane.connectors.fortios.redaction import scrub, scrub_text
from meho_backplane.connectors.fortios.target import (
    FortiOSTargetLike,
    load_token_from_vault,
    require_scope,
    resolve_scope,
)
from meho_backplane.connectors.fortios.versions import require_covered, version_warning
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["API_BASE", "FortiOSConnector", "TokenLoader", "describe_failure"]

_log = structlog.get_logger(__name__)

#: Every FortiOS REST path starts here (``cmdb/...``, ``monitor/...``, ``log/...``).
API_BASE: Final[str] = "/api/v2"

STATUS_PATH: Final[str] = "monitor/system/status"
FIRMWARE_PATH: Final[str] = "monitor/system/firmware"
HA_PEER_PATH: Final[str] = "monitor/system/ha-peer"
HA_CHECKSUMS_PATH: Final[str] = "monitor/system/ha-checksums"
HA_SETTINGS_PATH: Final[str] = "cmdb/system/ha"
VDOM_PATH: Final[str] = "cmdb/system/vdom"
CENTRAL_MANAGEMENT_PATH: Final[str] = "monitor/system/central-management/status"
LICENSE_PATH: Final[str] = "monitor/license/status"
RESOURCE_USAGE_PATH: Final[str] = "monitor/system/resource/usage"

#: Query keys only the connector sets: they pick the VDOM of a call.
_RESERVED_QUERY_KEYS: Final[frozenset[str]] = frozenset({"vdom", "global"})

#: Reads the API token of a target; tests pass a fake.
TokenLoader = Callable[[FortiOSTargetLike, Operator], Awaitable[str]]


def _is_tls_failure(exc: BaseException) -> bool:
    return isinstance(exc, httpx.ConnectError) and (
        isinstance(exc.__cause__, ssl.SSLCertVerificationError)
        or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def describe_failure(exc: BaseException, target: Any) -> str:
    """A clear, secret-free text for a failed call, for detection and probe results."""
    host = getattr(target, "host", None)
    if _is_tls_failure(exc):
        return (
            f"TLS check failed for {host!r}: MEHO does not trust the FortiGate's certificate. "
            "Pin the certificate or its CA with tls_ca_pin, and set tls_server_name when the "
            "target's host is an IP address. MEHO keeps the TLS check on."
        )
    if isinstance(
        exc,
        ConnectorAuthError
        | FortiOSApiError
        | FortiOSTargetError
        | FortiOSVersionError
        | CredentialsReadError,
    ):
        return scrub_text(str(exc))
    return scrub_text(f"{type(exc).__name__}: {exc}")


class FortiOSConnector(HttpConnector):
    """FortiGate REST connector: token login, one VDOM per target, checked answers."""

    product = "fortigate"
    version = "7.x"
    impl_id = "fortigate-rest"
    supported_version_range = ">=7.2,<8.0"
    priority = 1

    def __init__(self, *, token_loader: TokenLoader | None = None) -> None:
        super().__init__()
        self._tokens: dict[tuple[str, str], str] = {}
        self._token_lock = asyncio.Lock()
        self._token_loader: TokenLoader = (
            token_loader if token_loader is not None else load_token_from_vault
        )

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _token(self, target: FortiOSTargetLike, operator: Operator) -> str:
        """The cached token; the system operator always reads Vault again."""
        key = target_cache_key(target)
        async with self._token_lock:
            cached = self._tokens.get(key)
            if cached is not None and not is_system_operator(operator):
                return cached
            token = str(await self._token_loader(target, operator))
            self._tokens[key] = token
            _log.info("fortios_token_loaded", target=target.name)
            return token

    async def auth_headers(self, target: FortiOSTargetLike, operator: Operator) -> dict[str, str]:
        """``Authorization: Bearer <token>``; only the shared service account model."""
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"FortiOSConnector supports only auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target {target.name!r} "
                f"requested auth_model={auth_model!r}"
            )
        return {"Authorization": f"Bearer {await self._token(target, operator)}"}

    async def invalidate_credentials(self, target: FortiOSTargetLike) -> None:
        """Drop the cached token, so the next call reads Vault again (after a 401)."""
        async with self._token_lock:
            self._tokens.pop(target_cache_key(target), None)

    async def aclose(self) -> None:
        async with self._token_lock:
            self._tokens.clear()
        await super().aclose()

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    @staticmethod
    def _query(target: Any, query: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(query or {})
        reserved = sorted(k for k in params if str(k).lower() in _RESERVED_QUERY_KEYS)
        if reserved:
            raise FortiOSScopeError(
                f"the query may not set {reserved}: the connector sends the target's VDOM "
                "on every call"
            )
        params["vdom"] = resolve_scope(target).vdom
        return params

    async def api_get(
        self,
        target: FortiOSTargetLike,
        operator: Operator,
        api_path: str,
        *,
        query: Mapping[str, Any] | None = None,
    ) -> FortiOSAnswer:
        """``GET /api/v2/<api_path>`` with the target's VDOM; return the checked answer."""
        params = self._query(target, query)
        try:
            payload = await self._get_json(
                target, f"{API_BASE}/{api_path}", operator=operator, params=params
            )
        except httpx.HTTPStatusError as exc:
            raise error_from_status(exc, target=target, api_path=api_path) from None
        return unwrap_answer(payload, api_path)

    async def api_write(
        self,
        target: FortiOSTargetLike,
        operator: Operator,
        method: str,
        api_path: str,
        *,
        body: dict[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> FortiOSAnswer:
        """``POST`` / ``PUT`` / ``DELETE`` with the target's VDOM, sent once, never retried.

        A transport error (a timeout, a lost connection) is raised as it is;
        :func:`~meho_backplane.connectors.fortios.write_checks.send_write`
        turns it into the ``unknown`` outcome.
        """
        params = self._query(target, query)
        try:
            payload = await self._post_json(
                target,
                f"{API_BASE}/{api_path}",
                operator=operator,
                verb=method,
                params=params,
                json=body,
            )
        except httpx.HTTPStatusError as exc:
            raise error_from_status(exc, target=target, api_path=api_path) from None
        return unwrap_answer(payload, api_path)

    def trace_response_body(self, response: httpx.Response) -> Any:
        """The stored trace keeps the answer only with its secrets removed."""
        try:
            payload = json.loads(response.content or b"null")
        except ValueError:
            return scrub_text(response.text)
        cleaned, _ = scrub(payload)
        return cleaned

    async def _optional(
        self, target: FortiOSTargetLike, operator: Operator, api_path: str
    ) -> FortiOSAnswer | None:
        """A detection detail: ``None`` when the token may not read it or FortiOS refuses."""
        try:
            return await self.api_get(target, operator, api_path)
        except (FortiOSApiError, ConnectorAuthError) as exc:
            if isinstance(exc, ConnectorAuthError) and exc.status_code == 401:
                raise
            return None

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _result(
        self, *, reachable: bool, extras: dict[str, Any], **fields: Any
    ) -> FingerprintResult:
        return FingerprintResult(
            vendor="fortinet",
            product="fortigate",
            reachable=reachable,
            probed_at=datetime.now(UTC),
            probe_method=f"GET {API_BASE}/{STATUS_PATH}",
            extras=extras,
            **fields,
        )

    async def fingerprint(
        self, target: FortiOSTargetLike, operator: Operator | None = None
    ) -> FingerprintResult:
        """Detect the device: model, serial, hardware or VM, version, HA, VDOMs, FortiManager.

        A non-FortiGate model or a version outside FortiOS 7.2 to 7.x is
        refused: ``reachable=False`` with the reason under ``extras.error``
        and ``extras.refused=True``. The token is never part of the result.
        """
        effective = operator or synthesise_system_operator()
        try:
            scope = resolve_scope(target)
            status = await self.api_get(target, effective, STATUS_PATH)
            firmware = await self.api_get(target, effective, FIRMWARE_PATH)
        except (
            httpx.HTTPError,
            OSError,
            VaultClientError,
            CredentialsReadError,
            ConnectorAuthError,
            FortiOSApiError,
            FortiOSTargetError,
        ) as exc:
            return self._result(reachable=False, extras={"error": describe_failure(exc, target)})
        refusal = _refusal(status)
        if refusal is not None:
            return self._result(reachable=False, extras=refusal)
        try:
            peers = await self._optional(target, effective, HA_PEER_PATH)
            vdoms = await self._optional(target, effective, VDOM_PATH)
            fmg = await self._optional(target, effective, CENTRAL_MANAGEMENT_PATH)
        except (httpx.HTTPError, ConnectorAuthError) as exc:
            return self._result(reachable=False, extras={"error": describe_failure(exc, target)})
        version = require_covered(status.version)
        members = reads.ha_members(peers.results) if peers is not None else None
        fields = reads.status_fields(status.results, firmware.results)
        extras: dict[str, Any] = {
            **fields,
            "serial": status.serial,
            "scope": scope.kind,
            "vdom": scope.vdom,
            "ha_members": members,
            "device_id": reads.device_identity(members or [], status.serial),
            "vdoms": [r["name"] for r in reads.vdom_rows(vdoms.results)] if vdoms else None,
            "fortimanager_controls": (
                reads.fortimanager_state(fmg.results)["fortimanager_controls"] if fmg else None
            ),
            "version_warning": version_warning(status.version),
        }
        return self._result(
            reachable=True,
            version=".".join(map(str, version)),
            build=str(status.build) if status.build is not None else None,
            edition=fields["platform"],
            extras=extras,
        )

    async def probe(self, target: FortiOSTargetLike) -> ProbeResult:
        """Reachability through :meth:`fingerprint` (no operator: the system operator)."""
        probed_at = datetime.now(UTC)
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=probed_at)
        return ProbeResult(ok=False, reason=str(fp.extras.get("error")), probed_at=probed_at)

    async def execute(
        self, target: FortiOSTargetLike, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        """Old entry point; runs the operation through the dispatcher as the system operator."""
        from meho_backplane.operations import dispatch

        return await dispatch(
            operator=synthesise_system_operator(),
            connector_id=f"{self.impl_id}-{self.version}",
            op_id=op_id,
            target=target,
            params=params,
        )

    # ------------------------------------------------------------------
    # Device reads (all need a device target)
    # ------------------------------------------------------------------

    async def _device_get(
        self,
        target: FortiOSTargetLike,
        operator: Operator,
        op_id: str,
        *paths: str,
        query: Mapping[str, Any] | None = None,
    ) -> tuple[list[FortiOSAnswer], str | None]:
        require_scope(target, op_id, "device")
        answers = [await self.api_get(target, operator, p, query=query) for p in paths]
        return answers, version_warning(answers[0].version)

    async def system_status(
        self, operator: Operator, target: FortiOSTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fortios.system.status``."""
        del params
        (status, firmware, licence), warning = await self._device_get(
            target, operator, "fortios.system.status", STATUS_PATH, FIRMWARE_PATH, LICENSE_PATH
        )
        licences = {row["service"]: row["status"] for row in reads.licence_rows(licence.results)}
        return {
            **reads.status_fields(status.results, firmware.results),
            "serial": status.serial,
            "version": status.version,
            "build": status.build,
            "forticare_status": licences.get("forticare"),
            "vm_licence_status": licences.get("vm"),
            "version_warning": warning,
        }

    async def ha_status(
        self, operator: Operator, target: FortiOSTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fortios.ha.status``."""
        del params
        (peers, sums, settings), warning = await self._device_get(
            target, operator, "fortios.ha.status", HA_PEER_PATH, HA_CHECKSUMS_PATH, HA_SETTINGS_PATH
        )
        members = reads.ha_members(peers.results)
        rows = reads.checksums(sums.results)
        return {
            **reads.ha_settings(settings.results),
            "member_count": len(members),
            "members": members,
            "in_sync": reads.ha_in_sync(rows),
            "checksums": rows,
            "device_id": reads.device_identity(members, peers.serial),
            "version_warning": warning,
        }

    async def vdom_list(
        self, operator: Operator, target: FortiOSTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fortios.vdom.list``."""
        del params
        (answer,), warning = await self._device_get(
            target, operator, "fortios.vdom.list", VDOM_PATH
        )
        rows = reads.vdom_rows(answer.results)
        return {"count": len(rows), "vdoms": rows, "version_warning": warning}

    async def central_management_status(
        self, operator: Operator, target: FortiOSTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fortios.central_management.status``."""
        del params
        (answer,), warning = await self._device_get(
            target, operator, "fortios.central_management.status", CENTRAL_MANAGEMENT_PATH
        )
        return {**reads.fortimanager_state(answer.results), "version_warning": warning}

    async def license_status(
        self, operator: Operator, target: FortiOSTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fortios.license.status``."""
        del params
        (answer,), warning = await self._device_get(
            target, operator, "fortios.license.status", LICENSE_PATH
        )
        rows = reads.licence_rows(answer.results)
        return {"count": len(rows), "licences": rows, "version_warning": warning}

    async def resource_usage(
        self, operator: Operator, target: FortiOSTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fortios.resource.usage`` (device-wide: ``scope=global``)."""
        del params
        (answer,), warning = await self._device_get(
            target,
            operator,
            "fortios.resource.usage",
            RESOURCE_USAGE_PATH,
            query={"scope": "global"},
        )
        return {**reads.resource_usage(answer.results), "version_warning": warning}


def _refusal(status: FortiOSAnswer) -> dict[str, Any] | None:
    """The refusal extras when the device is not a covered FortiGate, else ``None``."""
    model_name = status.results.get("model_name") if isinstance(status.results, dict) else None
    if not reads.is_fortigate(status.results):
        return {
            "error": (
                f"the device reports model_name {model_name!r}, not a FortiGate; this "
                "connector covers FortiGate only"
            ),
            "refused": True,
            "detected_model_name": model_name,
            "detected_version": status.version,
        }
    try:
        require_covered(status.version)
    except FortiOSVersionError as exc:
        return {
            "error": str(exc),
            "refused": True,
            "detected_model_name": model_name,
            "detected_version": status.version,
        }
    return None
