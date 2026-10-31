# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CloudBuilderConnector — the Cloud Builder (VCF 5.x bring-up) implementation of ``installer``.

VCF 5.0 to 5.2 are brought up with the Cloud Builder appliance. Its REST API
(``/v1/sddcs``) is the ancestor of the VCF Installer API, so this connector is
a second implementation of the ``installer`` product, next to
``installer-rest-9.1``:

* ``installer-cb-5.0`` (this class): ``>=5.0,<9.0``, registered without a
  wildcard;
* ``installer-rest-9.1``: ``>=9.0,<10.0``, and it keeps the
  ``("installer", "", "")`` wildcard, so a target with no version still goes
  there. Set the target's version (for example ``5.2``) for a Cloud Builder.

Login
-----

Cloud Builder uses HTTP Basic with its ``admin`` account on every call. There
is no token call. The account comes from the target's Vault path
(``{username, password}``) and is kept in memory per target. When Cloud
Builder answers ``401``, the connector reads the account from Vault again and
sends the call once more. A ``401`` means the call was refused before Cloud
Builder did anything, so sending it again is safe, also for a start.

Version detection
-----------------

``GET /v1/sddcs/about`` returns the bring-up service with its ``version``.
The same call is the ``installer.cb.about`` operation.

Secrets in later reads
----------------------

A start sends the spec's passwords and licence keys; a vendor error text of
a later status read can echo one. The connector remembers, by the target's
host, the values each start read and the ids of the jobs it started. A status
read of a job it started, and the flight-recorder body of that answer, hide
the remembered values. A status read of a job it did not start (one from
before a restart of MEHO, or started by someone else) shows no vendor error
text at all, because MEHO cannot know which values such a text may echo.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import (
    basic_auth_header,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector, _read_capped_json_response
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vcf_cloud_builder._jobs import (
    SecretMemory,
    hide_secrets_everywhere,
    hide_vendor_texts,
)
from meho_backplane.connectors.vcf_cloud_builder.typed_reads import ABOUT_PATH
from meho_backplane.connectors.vcf_installer.session import (
    InstallerCredentialsLoader,
    InstallerTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.flight_recorder import capture as flight_recorder_capture

__all__ = ["CloudBuilderConnector"]

_log = structlog.get_logger(__name__)

#: The status Cloud Builder sends when it does not accept the login.
_LOGIN_REFUSED = 401

#: The path of one validation or one bring-up; group 1 is the job id.
_JOB_PATH = re.compile(r"^/v1/sddcs/(?:validations/)?([^/]+)$")

#: The keys of a vendor ``Error`` that hold text.
_VENDOR_TEXT_KEYS = ("message", "remediationMessage")


class CloudBuilderConnector(HttpConnector):
    """Cloud Builder 5.x REST connector with HTTP Basic login."""

    product = "installer"
    version = "5.0"
    impl_id = "installer-cb"
    supported_version_range = ">=5.0,<9.0"
    priority = 1

    def __init__(self, *, credentials_loader: InstallerCredentialsLoader | None = None) -> None:
        super().__init__()
        self._creds_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: InstallerCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._secret_memory = SecretMemory()

    def remember_secrets(self, target: InstallerTargetLike, values: Iterable[str]) -> None:
        """Keep the secret values a start sent to *target*, to hide them in later reads."""
        self._secret_memory.remember(target.host, values)

    def known_secrets(self, target: InstallerTargetLike) -> tuple[str, ...]:
        """The secret values the starts sent to *target* since MEHO started."""
        return self._secret_memory.known(target.host)

    def remember_job(self, target: InstallerTargetLike, job_id: object) -> None:
        """Note that a start of this MEHO created the job *job_id* on *target*."""
        self._secret_memory.remember_job(target.host, job_id)

    def knows_job(self, target: InstallerTargetLike, job_id: object) -> bool:
        """``True`` when a start of this MEHO created the job *job_id* on *target*."""
        return self._secret_memory.knows_job(target.host, job_id)

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Hide the secrets before the trace records a body.

        The flight recorder's own rules know secrets by shape (a token, a
        key file) and by op family (a login). A spec password that a
        validation check echoes is neither, so the connector hides it here
        with the same rules the status reads use: the remembered values and
        every licence key are hidden, and for a job this MEHO did not start
        every vendor error text is hidden. A body that is not JSON is
        recorded as the shared engine sees fit.
        """
        try:
            payload = response.json()
        except ValueError:
            return None
        url = getattr(getattr(response, "request", None), "url", None)
        host = getattr(url, "host", "")
        match = _JOB_PATH.match(str(getattr(url, "path", "")))
        hidden = hide_secrets_everywhere(payload, self._secret_memory.known(host))
        if match and self._secret_memory.knows_job(host, match.group(1)):
            return hidden
        return hide_vendor_texts(hidden, _VENDOR_TEXT_KEYS)

    async def auth_headers(self, target: InstallerTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Basic <b64>"}`` for the Cloud Builder ``admin`` account."""
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"CloudBuilderConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target, operator)
        return {"Authorization": basic_auth_header(creds["username"], creds["password"])}

    async def _load_credentials(
        self, target: InstallerTargetLike, operator: Operator
    ) -> dict[str, str]:
        """The cached ``{username, password}`` of *target*, read from Vault on first use.

        The system operator never reads from or writes to the cache.
        """
        cache_key = target_cache_key(target)
        async with self._creds_lock:
            cached = self._creds_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            raw = await self._credentials_loader(target, operator)
            for key in ("username", "password"):
                if not raw.get(key):
                    raise RuntimeError(
                        f"cloud-builder credentials for target {target.name!r} have no "
                        f"{key!r}; store {{'username': ..., 'password': ...}} at the "
                        "target's Vault path"
                    )
            if not is_system_operator(operator):
                self._creds_cache[cache_key] = raw
            _log.info("cloud_builder_credentials_loaded", target=target.name, host=target.host)
            return raw

    async def invalidate_credentials(self, target: InstallerTargetLike) -> None:
        """Forget the cached account of *target*; the next call reads Vault again."""
        async with self._creds_lock:
            self._creds_cache.pop(target_cache_key(target), None)

    async def send(
        self,
        target: InstallerTargetLike,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one JSON call; on a ``401``, read the account again and send it once more."""
        for attempt in (1, 2):
            try:
                if method == "GET":
                    return await self._get_json(target, path, operator=operator, params=params)
                return await self._post_json(
                    target, path, operator=operator, verb=method, params=params, json=json
                )
            except httpx.HTTPStatusError as exc:
                if attempt == 2 or exc.response.status_code != _LOGIN_REFUSED:
                    raise
                await self.invalidate_credentials(target)
        raise AssertionError("unreachable")  # pragma: no cover

    async def get_file(
        self,
        target: InstallerTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """GET a file (a report). Same login retry as :meth:`send`; same size cap as JSON calls."""
        for attempt in (1, 2):
            response = await self._get_file_once(target, path, operator=operator, params=params)
            if response.status_code != _LOGIN_REFUSED or attempt == 2:
                response.raise_for_status()
                return response
            await self.invalidate_credentials(target)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _get_file_once(
        self,
        target: InstallerTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None,
    ) -> httpx.Response:
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        span = flight_recorder_capture.span_start()
        try:
            response = await _read_capped_json_response(
                client,
                "GET",
                path,
                params=params,
                headers=headers,
                extensions=self._request_extensions(target),
            )
        except httpx.TransportError as exc:
            flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
            raise
        flight_recorder_capture.record_vendor_call(
            span,
            method="GET",
            request_headers=headers,
            response=response,
            request_body=None,
            request_content_type=None,
            response_body=None,
        )
        return response

    async def fingerprint(
        self, target: InstallerTargetLike, operator: Operator | None = None
    ) -> FingerprintResult:
        """Version detection from ``GET /v1/sddcs/about`` (``VcfService.version``)."""
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        method = f"GET {ABOUT_PATH}"
        try:
            payload = await self.send(target, "GET", ABOUT_PATH, operator=eff_operator)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="installer",
                reachable=False,
                probed_at=probed_at,
                probe_method=method,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="vmware",
            product="installer",
            version=payload.get("version"),
            build=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=method,
            extras={"service": payload.get("name"), "status": payload.get("status")},
        )

    async def probe(self, target: InstallerTargetLike) -> ProbeResult:
        """Reachability and login check; uses :meth:`fingerprint`."""
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False, reason=str(fp.extras.get("error", "unreachable")), probed_at=fp.probed_at
        )

    async def execute(
        self, target: InstallerTargetLike, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        """Old entry point; runs the operation through the dispatcher."""
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:installer-cb-connector-shim",
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

    # --- Operation handlers (bodies in typed_reads / typed_writes) ---------------

    async def about(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.about``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_reads import about_impl

        return await about_impl(self, operator, target, params)

    async def validation_start(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.validation.start``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_writes import (
            validation_start_impl,
        )

        return await validation_start_impl(self, operator, target, params)

    async def validation_status(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.validation.status``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_reads import (
            validation_status_impl,
        )

        return await validation_status_impl(self, operator, target, params)

    async def sddc_start(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.sddc.start``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_writes import sddc_start_impl

        return await sddc_start_impl(self, operator, target, params)

    async def sddc_status(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.sddc.status``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_reads import sddc_status_impl

        return await sddc_status_impl(self, operator, target, params)

    async def sddc_retry(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.sddc.retry``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_writes import sddc_retry_impl

        return await sddc_retry_impl(self, operator, target, params)

    async def sddc_report(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.cb.sddc.report``."""
        from meho_backplane.connectors.vcf_cloud_builder.typed_reads import sddc_report_impl

        return await sddc_report_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Forget cached accounts and remembered secrets, then close the HTTP pool."""
        async with self._creds_lock:
            self._creds_cache.clear()
        self._secret_memory.clear()
        await super().aclose()
