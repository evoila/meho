# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""GCP Secret Manager credential backend (SA-direct, #2230).

The second :class:`~meho_backplane.connectors._shared.credential_backend.CredentialBackend`
after Vault — registered under kind ``"gsm"`` on the #2229 resolver seam so
a GCP-native adopter can point a target at
``gsm:<project-id>/<secret-name>[#field]`` and have MEHO resolve the
credential with **no Vault**.

Auth model — SA-direct under GKE Workload Identity
==================================================

Phase 1 reads Secret Manager under **MEHO's own** identity: the pod's
Application Default Credentials (``google.auth.default()``), which on GKE
resolve to the deployment's Workload Identity service account. An optional
deployment-level SA (``config.gsmImpersonateSa`` / ``GSM_IMPERSONATE_SA``)
wraps that ADC source in
``google.auth.impersonated_credentials.Credentials`` — the exact ADC +
impersonation chain :class:`~meho_backplane.connectors.gcloud.connector.GcloudConnector`
already drives (``gcloud/connector.py:_fetch_token_sync``). No
service-account JSON key ever enters the flow, honouring the consumer
org's ``constraints/iam.disableServiceAccountKeyCreation`` policy the same
way the GCloud connector does: by never using key material at all.

Per-operator GCP federation (STS token exchange, #2232) is **out of
scope** here. MEHO's own audit attribution is unaffected — the audit row
still carries the Keycloak ``sub`` (the policy/audit seam is untouched by
this backend). What Phase 1 does not yet have is *GCP-layer* per-operator
attribution: every GSM read is attributed to MEHO's SA, not the operator.

Ref grammar and field selection
===============================

The scheme-stripped store ref (the part after ``gsm:``) is
``<project-id>/<secret-name>[/versions/<version>][#<field>]``:

* bare — ``proj/secret`` — reads the **latest** version and returns the
  whole JSON payload as the secret-field dict.
* pinned — ``proj/secret/versions/5`` — reads that exact version.
* ``#field`` — ``proj/secret#password`` — reads the payload, JSON-decodes
  it, and returns just the named field (``{field: value}``). A payload
  that is not a JSON object, or a missing field, raises a clear error —
  mirroring the shared loader's ``_extract_fields`` contract.

Both forms require the decoded payload to be a JSON **object**: the
backend contract returns a named-field ``dict`` the shared loader's
extraction consumes, exactly as the Vault backend returns a KV-v2 data
dict. This keeps a GSM secret interchangeable with a Vault secret for a
connector session builder — it reads the same ``{username, password}``
shape regardless of store.

No secret in logs
=================

The structlog event carries only ``target`` / ``project`` / ``secret`` /
``version`` / the requested ``field`` name — never a credential value,
matching the no-secret discipline of ``vault_creds`` (``vault_creds.py``
logs field *names* only).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from meho_backplane.connectors._shared.credential_backend import register_credential_backend
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from meho_backplane.auth.operator import Operator

__all__ = [
    "GcpSecretManagerBackend",
    "GcpSecretManagerReadError",
]

_log = structlog.get_logger(__name__)

#: Full-access GCP scope the impersonated / ADC credentials request — the
#: same scope the GCloud connector uses. Secret Manager's ``versions.access``
#: permission is covered by ``cloud-platform``.
_GCP_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: Seconds an impersonated token lives — GCP's maximum for impersonated
#: credentials, matching ``gcloud/connector.py``.
_IMPERSONATION_LIFETIME = 3600

#: Default version alias when the ref pins no explicit version.
_LATEST_VERSION = "latest"


class GcpSecretManagerReadError(Exception):
    """Read-phase failure resolving a ``gsm:`` credential ref.

    The GSM analogue of
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`:
    a distinct, actionable error raised when the ref is malformed, the ADC
    source is empty, the project/secret is unreachable, access is denied,
    or the payload is not a JSON object / is missing the requested field.
    Never a bare ``google.api_core`` exception surfacing from deep in the
    client, and never echoing a credential value — the message names the
    project / secret / field only.
    """


class _SecretAccessResult(Protocol):
    """Structural shape of ``access_secret_version``'s response payload.

    Lets the unit tests hand a plain stub client back through
    ``client_factory`` without importing the generated proto types.
    """

    @property
    def payload(self) -> Any: ...

    @property
    def name(self) -> str: ...


def _parse_gsm_ref(store_ref: str, *, target_name: str) -> tuple[str, str, str, str | None]:
    """Split a scheme-stripped GSM ref into ``(project, secret, version, field)``.

    Grammar: ``<project>/<secret>[/versions/<version>][#<field>]``. The
    optional ``#field`` fragment is split on the **last** ``#`` (mirroring
    the vault-kv broker's ``_parse_vault_ref``); ``version`` defaults to
    ``"latest"`` when the ref pins none. A malformed ref (wrong segment
    count, empty project/secret/version, empty field after ``#``) raises
    :class:`GcpSecretManagerReadError` naming the target — never a bare
    ``ValueError`` / ``IndexError``.
    """
    body, sep, field_part = store_ref.rpartition("#")
    if sep:
        field: str | None = field_part.strip()
        path = body.strip()
        if not path or not field:
            raise GcpSecretManagerReadError(
                f"target {target_name!r} has a malformed gsm secret_ref {store_ref!r}: "
                "the '#<field>' fragment selects one JSON key and needs a non-empty "
                "path and field (e.g. 'my-project/db-creds#password')"
            )
    else:
        field = None
        path = store_ref.strip()

    segments = path.split("/")
    if len(segments) == 2:
        project, secret = segments
        version = _LATEST_VERSION
    elif len(segments) == 4 and segments[2] == "versions":
        project, secret, _, version = segments
    else:
        raise GcpSecretManagerReadError(
            f"target {target_name!r} has a malformed gsm secret_ref {store_ref!r}: "
            "expected '<project-id>/<secret-name>[/versions/<version>][#<field>]' "
            "(e.g. 'my-project/db-creds' or 'my-project/db-creds/versions/3#password')"
        )
    if not project or not secret or not version:
        raise GcpSecretManagerReadError(
            f"target {target_name!r} has a gsm secret_ref {store_ref!r} with an empty "
            "project, secret name, or version segment"
        )
    return project, secret, version, field


def _decode_payload(
    payload_bytes: bytes,
    *,
    field: str | None,
    target_name: str,
    project: str,
    secret: str,
) -> dict[str, object]:
    """Decode a GSM payload to the named-field dict the seam contract returns.

    Both the bare and ``#field`` forms require the payload to be a JSON
    **object**: the shared loader extracts named fields from the returned
    dict exactly as it does for a Vault KV-v2 data dict, so a scalar / list
    / non-JSON payload cannot satisfy the contract and raises a clear
    error. The bare form returns the whole object; the ``#field`` form
    returns ``{field: value}`` after asserting the field is present. The
    error message names project / secret / field only — never the value.
    """
    try:
        decoded = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GcpSecretManagerReadError(
            f"gsm secret {project}/{secret} for target {target_name!r} is not "
            "valid UTF-8 JSON; a MEHO credential secret must store a JSON object "
            'of named fields (e.g. \'{"username": ..., "password": ...}\')'
        ) from exc

    if not isinstance(decoded, dict):
        raise GcpSecretManagerReadError(
            f"gsm secret {project}/{secret} for target {target_name!r} decoded to "
            f"{type(decoded).__name__}, not a JSON object; a MEHO credential secret "
            "must store a JSON object of named fields"
        )

    if field is None:
        return decoded

    if field not in decoded:
        raise GcpSecretManagerReadError(
            f"gsm secret {project}/{secret} for target {target_name!r} has no field {field!r}"
        )
    return {field: decoded[field]}


class GcpSecretManagerBackend:
    """Resolve ``gsm:`` refs via GCP Secret Manager under SA-direct ADC.

    Registered under kind ``"gsm"``. Stateless apart from the injected test
    seams, so a single shared instance serves every read.

    The two injection points keep the unit tests off live GCP:

    * ``adc_loader`` replaces ``google.auth.default`` (returns
      ``(credentials, project)``), so a test supplies a fake ADC source.
    * ``client_factory`` replaces ``SecretManagerServiceClient`` (called
      with ``credentials=``), so a test returns a stub whose
      ``access_secret_version`` yields a canned payload without any RPC.

    ``impersonate_sa`` overrides the settings-derived impersonation SA
    (``None`` ⇒ read ``Settings.gsm_impersonate_sa`` at call time, matching
    how ``vault_creds`` reads ``credential_backend`` per-call).
    """

    def __init__(
        self,
        *,
        adc_loader: Callable[..., tuple[Any, Any]] | None = None,
        client_factory: Callable[..., Any] | None = None,
        impersonate_sa: str | None = None,
    ) -> None:
        self._adc_loader = adc_loader
        self._client_factory = client_factory
        self._impersonate_sa = impersonate_sa

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str = "",
    ) -> dict[str, object]:
        """Read *secret_ref* from GCP Secret Manager and return its field dict.

        ``operator`` is accepted for the
        :class:`~meho_backplane.connectors._shared.credential_backend.CredentialBackend`
        contract but not forwarded to GCP — Phase 1 reads under MEHO's own
        ADC identity, not the operator's (per-operator GCP federation is
        #2232). ``mount`` is a Vault-KV concept with no GSM analogue and is
        ignored. The synchronous ``access_secret_version`` RPC runs off the
        event loop via ``asyncio.to_thread``, matching the hvac precedent in
        ``vault_creds``.
        """
        project, secret, version, field = _parse_gsm_ref(secret_ref, target_name=target_name)

        payload_bytes, resolved_name = await asyncio.to_thread(
            self._access_sync, project, secret, version, target_name
        )

        secret_data = _decode_payload(
            payload_bytes,
            field=field,
            target_name=target_name,
            project=project,
            secret=secret,
        )

        # Non-secret attribution only: target / project / secret / the
        # resolved version name / the requested field name. Never a value.
        _log.info(
            "gsm_secret_accessed",
            target=target_name,
            project=project,
            secret_name=secret,
            version=resolved_name or version,
            field=field,
        )
        return secret_data

    # ------------------------------------------------------------------
    # Synchronous GCP access (runs in a worker thread)
    # ------------------------------------------------------------------

    def _access_sync(
        self, project: str, secret: str, version: str, target_name: str
    ) -> tuple[bytes, str]:
        """Build credentials, access the secret version, return ``(bytes, name)``.

        Runs in a thread (``asyncio.to_thread``) because the google-cloud
        client and ``google.auth`` credential refresh both perform blocking
        transport under the hood. Access-denied, not-found, and transport
        failures are re-raised as :class:`GcpSecretManagerReadError` with an
        actionable message naming project / secret (AC #5) — not a bare
        ``google.api_core`` exception.
        """
        import google.api_core.exceptions as gcp_exceptions
        from google.cloud import secretmanager

        credentials = self._build_credentials(target_name)
        factory = self._client_factory or secretmanager.SecretManagerServiceClient
        client = factory(credentials=credentials)

        name = f"projects/{project}/secrets/{secret}/versions/{version}"
        try:
            response: _SecretAccessResult = client.access_secret_version(name=name)
        except gcp_exceptions.PermissionDenied as exc:
            raise GcpSecretManagerReadError(
                f"access denied reading gsm secret {project}/{secret} for target "
                f"{target_name!r}: MEHO's identity lacks 'secretmanager.versions.access' "
                "on the secret (grant roles/secretmanager.secretAccessor)"
            ) from exc
        except gcp_exceptions.NotFound as exc:
            raise GcpSecretManagerReadError(
                f"gsm secret version {name} for target {target_name!r} was not found: "
                "check the project id, secret name, and version"
            ) from exc
        except gcp_exceptions.GoogleAPICallError as exc:
            raise GcpSecretManagerReadError(
                f"gsm secret {project}/{secret} for target {target_name!r} could not be "
                f"read: {exc.__class__.__name__} from Secret Manager"
            ) from exc

        payload = getattr(response, "payload", None)
        data = getattr(payload, "data", None)
        if not isinstance(data, (bytes, bytearray)):
            raise GcpSecretManagerReadError(
                f"gsm secret {project}/{secret} for target {target_name!r} returned no payload data"
            )
        return bytes(data), str(getattr(response, "name", "") or "")

    def _build_credentials(self, target_name: str) -> Any:
        """Return ADC source credentials, optionally impersonating an SA.

        ``google.auth.default()`` yields the pod's ambient ADC (GKE Workload
        Identity on the target platform). A configured impersonation SA
        wraps that source in ``impersonated_credentials.Credentials`` — the
        GCloud connector's chain. Fails closed with
        :class:`GcpSecretManagerReadError` when ADC cannot be resolved or
        yields no credentials (AC #4).
        """
        import google.auth
        from google.auth import exceptions as auth_exceptions

        adc_loader = self._adc_loader or google.auth.default
        try:
            source_credentials, _project = adc_loader(scopes=[_GCP_CLOUD_PLATFORM_SCOPE])
        except auth_exceptions.DefaultCredentialsError as exc:
            raise GcpSecretManagerReadError(
                f"no Application Default Credentials available to read gsm secrets for "
                f"target {target_name!r}: MEHO must run under a GKE Workload Identity "
                "service account (or have GOOGLE_APPLICATION_CREDENTIALS set to a "
                "non-key-file credential)"
            ) from exc
        if source_credentials is None:
            raise GcpSecretManagerReadError(
                f"Application Default Credentials resolved to no credentials for target "
                f"{target_name!r}; cannot read gsm secrets"
            )

        impersonate_sa = self._resolve_impersonate_sa()
        if not impersonate_sa:
            return source_credentials

        import google.auth.impersonated_credentials

        return google.auth.impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
            source_credentials=source_credentials,
            target_principal=impersonate_sa,
            target_scopes=[_GCP_CLOUD_PLATFORM_SCOPE],
            lifetime=_IMPERSONATION_LIFETIME,
        )

    def _resolve_impersonate_sa(self) -> str:
        """The impersonation SA — the constructor override or the setting.

        ``None`` on the instance defers to ``Settings.gsm_impersonate_sa``
        read per-call (so a redeploy's config change is picked up without
        re-registering the backend). An empty string means direct ADC.
        """
        if self._impersonate_sa is not None:
            return self._impersonate_sa
        return get_settings().gsm_impersonate_sa.strip()


#: The GSM backend is stateless (test seams aside), so a single shared
#: instance serves every read. Registered at import time under ``"gsm"``;
#: the ``_shared`` package ``__init__`` imports this module so the kind is
#: present before any credential resolution runs.
register_credential_backend("gsm", GcpSecretManagerBackend())
