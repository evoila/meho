# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""GCP Secret Manager credential backend (SA-direct + WIF, #2230 / #2232).

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

Per-operator GCP federation — Workload Identity Federation (#2232)
=================================================================

Phase 2 adds a **per-operator** read path alongside the SA-direct one.
When Workload Identity Federation is configured (``GSM_WIF_AUDIENCE`` set),
a ``gsm:`` read exchanges the calling operator's validated Keycloak JWT
(``operator.raw_jwt``) at ``sts.googleapis.com`` for a short-lived
federated token via google-auth ``identity_pool.Credentials``, optionally
impersonates a target SA, then does the one ``secretmanager.versions.access``
and discards the token. This restores *GCP-layer* per-operator attribution
— GCP's own audit log names the operator, not MEHO's platform SA — mirroring
the Vault ``vault_client_for_operator`` JIT contract (``auth/vault.py:198``):
a fresh credential per operation, no caching across requests.

Selection is per-read: WIF configured **and** an operator JWT present ⇒ the
operator path; WIF unconfigured ⇒ the Phase-1 SA-direct ADC path, unchanged
(no behaviour change for Phase-1 installs).

Background dispatch (#2642)
===========================

WIF configured but an **empty** ``operator.raw_jwt`` — a background sensor
evaluation, the topology-refresh scheduler, runbook verify dispatch, a
legacy connector ``execute()`` shim — falls back to the SA-direct path
rather than failing closed (``_select_auth_path``, logged as
``auth_path="sa_direct_fallback"``). The shared loader used to reject such a
call before dispatch, which on a GCP-native install blocked a read the pod's
own identity could have served and left every credentialed Sensor stuck at
``unknown``. Each backend now owns that precondition, and this backend has a
deployment identity to fall back on. Note the relaxation is not scoped to
the check-runner: it applies to every system-initiated caller with an empty
``raw_jwt`` on a GSM deploy.

Connector ``probe()`` / ``fingerprint()`` are **not** in that set.
:func:`~meho_backplane.connectors._shared.system_operator.synthesise_system_operator`
gives them a non-empty placeholder ``raw_jwt`` (G3.10), so
``_select_auth_path`` returns ``"wif"`` and the exchange is attempted with a
string STS will reject. The read then fails with
:class:`GcpSecretManagerReadError` and the connector degrades to
``reachable=False`` / ``auth_failed``; the placeholder is a fixed non-secret
sentinel, so nothing leaks. Teaching ``_select_auth_path`` to treat the
placeholder as absent is a behaviour change tracked separately.

The fallback needs an ambient GCP identity, which an on-prem cluster does
not have. That deployment class instead gives the check-runner its own
Keycloak service principal
(:mod:`meho_backplane.auth.runner_identity`): the runner's synthetic
operator then carries a real JWT and the ordinary WIF exchange runs, with
GCP attributing the read to the check-runner principal. With neither, the
read fails closed with an error naming both remedies.

MEHO's own audit attribution is unchanged in both paths — the audit row
still carries the Keycloak ``sub`` (the policy/audit seam is untouched by
this backend). Phase 1 alone lacks *GCP-layer* per-operator attribution;
Phase 2's WIF path is what supplies it.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from meho_backplane.connectors._shared.credential_backend import (
    CredentialsReadError,
    register_credential_backend,
)
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

#: The GCP Security Token Service endpoint the WIF path exchanges the
#: operator JWT at (#2232). Pinned explicitly rather than deriving from
#: google-auth's ``token_url`` default so the exchange target is auditable
#: in this module and stable across google-auth versions.
_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"

#: Appended to every "no ambient GCP identity" error (#2642). On a
#: per-operator-WIF deploy this is the exact failure a background dispatch
#: hits, and the two ways out are not obvious from the ADC message alone:
#: give the pod an identity, or give the check-runner a principal whose JWT
#: can be federated.
_NO_IDENTITY_REMEDY = (
    "For background dispatch (sensor evaluations, health probes) on a "
    "per-operator-WIF deploy, either give the pod an ambient GCP identity or "
    "configure the check-runner service principal (CHECK_RUNNER_CLIENT_ID / "
    "CHECK_RUNNER_CLIENT_SECRET) so the runner has a JWT to exchange."
)

#: Template for the IAM Credentials ``generateAccessToken`` URL google-auth's
#: external-account flow calls to impersonate the target SA after the STS
#: exchange. ``projects/-`` lets IAM resolve the SA's project from its email.
_IAM_IMPERSONATION_URL_TEMPLATE = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    "{service_account}:generateAccessToken"
)


class GcpSecretManagerReadError(CredentialsReadError):
    """Read-phase failure resolving a ``gsm:`` credential ref.

    The GSM analogue of
    :class:`~meho_backplane.connectors._shared.vault_creds.VaultCredentialsReadError`:
    a distinct, actionable error raised when the ref is malformed, the ADC
    source is empty, the project/secret is unreachable, access is denied,
    or the payload is not a JSON object / is missing the requested field.
    Never a bare ``google.api_core`` exception surfacing from deep in the
    client, and never echoing a credential value — the message names the
    project / secret / field only.

    Every credential-read failure on a ``credentialBackend=gsm`` deploy now
    surfaces under **this** class (#2642). The dispatcher renders a handler
    exception as ``connector_error: <class name>``, and the shared loader
    used to fail system-initiated calls with the Vault-named class before a
    backend was even resolved — so a GCP-native install with ``vault:
    not_configured`` reported ``VaultCredentialsReadError`` and sent
    operators looking for a component they do not run.
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


class _OperatorJwtSubjectTokenSupplier:
    """Return the operator's Keycloak JWT as the WIF STS subject token.

    Implements the google-auth
    ``google.auth.identity_pool.SubjectTokenSupplier`` interface structurally
    (google-auth duck-types the supplier — it only calls
    :meth:`get_subject_token` — so no import of the abstract base is needed
    and the google imports stay lazy). One supplier is built per read and
    holds exactly that read's operator JWT, so the exchanged subject token is
    always the calling operator's — never a cached or shared token. The
    supplier itself does no caching, matching the JIT contract: identity-pool
    credentials do not cache the subject token, and a fresh
    ``identity_pool.Credentials`` is built per read.
    """

    def __init__(self, operator_jwt: str) -> None:
        self._operator_jwt = operator_jwt

    def get_subject_token(self, context: Any, request: Any) -> str:
        """Return the operator JWT (the ``context`` / ``request`` are unused).

        ``context`` (audience + subject-token type) and ``request`` (an HTTP
        transport) are part of the google-auth supplier signature but not
        needed here: the subject token is the already-validated bearer JWT the
        operator presented, forwarded bit-for-bit like the Vault OIDC login.
        """
        return self._operator_jwt


@dataclass(frozen=True)
class _WifConfig:
    """Resolved Workload Identity Federation settings for the operator path.

    Built from ``Settings`` by :func:`_resolve_wif_config`. ``audience`` is
    the full WIF provider resource name google-auth's ``identity_pool``
    consumes; ``pool_id`` / ``provider_id`` are the chart-facing
    ``gsm.workloadIdentityFederation.{poolId,providerId}`` keys, checked for
    consistency against the audience. ``service_account`` is the optional SA
    to impersonate for the final read; empty ⇒ no impersonation.
    """

    audience: str
    pool_id: str
    provider_id: str
    service_account: str
    subject_token_type: str


def _resolve_wif_config() -> _WifConfig | None:
    """Return the WIF config when configured, else ``None`` (SA-direct).

    WIF is "configured" when ``gsm_wif_audience`` is non-empty — that is the
    single value google-auth strictly needs (it encodes the pool + provider).
    An install that leaves it blank cleanly uses the Phase-1 SA-direct path,
    so a Phase-1 deployment sees no behaviour change.
    """
    settings = get_settings()
    audience = settings.gsm_wif_audience.strip()
    if not audience:
        return None
    return _WifConfig(
        audience=audience,
        pool_id=settings.gsm_wif_pool_id.strip(),
        provider_id=settings.gsm_wif_provider_id.strip(),
        service_account=settings.gsm_wif_service_account.strip(),
        subject_token_type=settings.gsm_wif_subject_token_type.strip(),
    )


def _select_auth_path(
    wif_config: _WifConfig | None, operator_jwt: str
) -> tuple[str, _WifConfig | None]:
    """Pick the credential path for one read: ``(label, active_wif_config)``.

    * WIF configured **and** a non-empty ``operator_jwt`` → ``"wif"``.
    * WIF unconfigured → ``"sa_direct"`` (the Phase-1 path, unchanged).
    * WIF configured but an **empty** ``operator_jwt`` →
      ``"sa_direct_fallback"`` (#2642).

    The third case is the one that matters. A system-initiated read (a
    background sensor evaluation with no check-runner principal configured,
    the topology-refresh scheduler, runbook verify dispatch, a legacy
    ``execute()`` shim) carries ``raw_jwt=""``, so there is nothing to
    federate with — but a deployment whose pod carries an ambient GCP
    identity (GKE Workload Identity, a mounted SA) can still read Secret
    Manager under it. Failing such a read closed bought nothing: it is not a
    privilege escalation (the pod identity is MEHO's own, and Phase-1
    installs read under it for every call), it just made scheduled
    evaluation impossible on deployments that had a perfectly good identity
    available.

    The test is truthiness, not "is this a real operator". Connector
    ``probe()`` / ``fingerprint()`` run under
    ``synthesise_system_operator()``, whose placeholder ``raw_jwt`` is
    deliberately non-empty (G3.10), so they take the ``"wif"`` branch and
    the exchange fails at STS — they do **not** get the fallback.

    The fallback is not silent — it rides its own ``auth_path`` label in the
    ``gsm_secret_accessed`` event, so an audit can tell a read GCP attributed
    to the operator from one attributed to MEHO's own identity. Deployments
    with no ambient ADC still fail closed, in ``_build_credentials``.
    """
    if wif_config is None:
        return "sa_direct", None
    if operator_jwt:
        return "wif", wif_config
    return "sa_direct_fallback", None


def _assert_wif_audience_consistent(wif_config: _WifConfig, target_name: str) -> None:
    """Fail closed when the declared pool / provider disagree with the audience.

    ``gsm_wif_audience`` is the value google-auth actually uses; ``pool_id`` /
    ``provider_id`` are the operator-facing chart keys. When either is set it
    must appear in the audience resource name at its exact segment — a
    mismatch is a copy-paste misconfiguration that would silently federate
    against the wrong pool, so it raises :class:`GcpSecretManagerReadError`
    naming the target rather than proceeding. Both empty ⇒ no check (the
    audience alone is authoritative).
    """
    audience = wif_config.audience
    pool_marker = f"/workloadIdentityPools/{wif_config.pool_id}/providers/"
    if wif_config.pool_id and pool_marker not in audience:
        raise GcpSecretManagerReadError(
            f"gsm WIF config for target {target_name!r} is inconsistent: "
            f"GSM_WIF_POOL_ID={wif_config.pool_id!r} is not the pool named in "
            "GSM_WIF_AUDIENCE; the audience and the declared pool must match"
        )
    if wif_config.provider_id and not audience.endswith(f"/providers/{wif_config.provider_id}"):
        raise GcpSecretManagerReadError(
            f"gsm WIF config for target {target_name!r} is inconsistent: "
            f"GSM_WIF_PROVIDER_ID={wif_config.provider_id!r} is not the provider named "
            "in GSM_WIF_AUDIENCE; the audience and the declared provider must match"
        )


class GcpSecretManagerBackend:
    """Resolve ``gsm:`` refs via GCP Secret Manager under SA-direct ADC.

    Registered under kind ``"gsm"``. Stateless apart from the injected test
    seams, so a single shared instance serves every read.

    Selection is per-read (#2232): when Workload Identity Federation is
    configured (``Settings.gsm_wif_audience`` set), the read runs under the
    **operator's** identity — the operator JWT is exchanged at the GCP STS
    for a short-lived federated token; otherwise the Phase-1 SA-direct ADC
    path runs unchanged.

    The injection points keep the unit tests off live GCP:

    * ``adc_loader`` replaces ``google.auth.default`` (returns
      ``(credentials, project)``), so a test supplies a fake ADC source.
    * ``client_factory`` replaces ``SecretManagerServiceClient`` (called
      with ``credentials=``), so a test returns a stub whose
      ``access_secret_version`` yields a canned payload without any RPC.
    * ``wif_credentials_factory`` replaces the real
      ``identity_pool.Credentials`` builder on the WIF path, so a test can
      assert the built credentials reach the client and that a fresh one is
      minted per read without a live STS exchange. ``None`` ⇒ the real
      builder (:meth:`_build_wif_credentials`).

    ``impersonate_sa`` overrides the settings-derived SA-direct impersonation
    SA (``None`` ⇒ read ``Settings.gsm_impersonate_sa`` at call time, matching
    how ``vault_creds`` reads ``credential_backend`` per-call). It applies to
    the SA-direct path only; the WIF path's SA impersonation is driven by
    ``Settings.gsm_wif_service_account``.
    """

    def __init__(
        self,
        *,
        adc_loader: Callable[..., tuple[Any, Any]] | None = None,
        client_factory: Callable[..., Any] | None = None,
        impersonate_sa: str | None = None,
        wif_credentials_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._adc_loader = adc_loader
        self._client_factory = client_factory
        self._impersonate_sa = impersonate_sa
        self._wif_credentials_factory = wif_credentials_factory

    async def load_secret_data(
        self,
        secret_ref: str,
        operator: Operator,
        *,
        target_name: str,
        mount: str = "",
    ) -> dict[str, object]:
        """Read *secret_ref* from GCP Secret Manager and return its field dict.

        When Workload Identity Federation is configured (#2232) and *operator*
        carries a JWT, the read runs under *operator*'s identity:
        ``operator.raw_jwt`` is exchanged at the GCP STS for a short-lived
        federated token so GCP audits the read to the operator. Otherwise
        Phase 1's SA-direct path reads under MEHO's own ADC identity and
        *operator* is unused — including as the #2642 fallback for a
        system-initiated read on a WIF-configured install.

        ``mount`` is a Vault-KV concept with no GSM analogue and is ignored.
        The synchronous ``access_secret_version`` RPC (and the blocking
        google-auth credential refresh / STS exchange it drives) runs off the
        event loop via ``asyncio.to_thread``, matching the hvac precedent in
        ``vault_creds``.
        """
        project, secret, version, field = _parse_gsm_ref(secret_ref, target_name=target_name)
        auth_path, active_wif = _select_auth_path(_resolve_wif_config(), operator.raw_jwt)

        payload_bytes, resolved_name = await asyncio.to_thread(
            self._access_sync,
            project,
            secret,
            version,
            target_name,
            operator.raw_jwt,
            active_wif,
        )

        secret_data = _decode_payload(
            payload_bytes,
            field=field,
            target_name=target_name,
            project=project,
            secret=secret,
        )

        # Non-secret attribution only: target / project / secret / the
        # resolved version name / the requested field name / which auth path
        # ran (and, on WIF, the pool + provider). Never a value, never a token.
        _log.info(
            "gsm_secret_accessed",
            target=target_name,
            project=project,
            secret_name=secret,
            version=resolved_name or version,
            field=field,
            auth_path=auth_path,
            wif_pool=active_wif.pool_id if active_wif is not None else None,
            wif_provider=active_wif.provider_id if active_wif is not None else None,
        )
        return secret_data

    # ------------------------------------------------------------------
    # Synchronous GCP access (runs in a worker thread)
    # ------------------------------------------------------------------

    def _access_sync(
        self,
        project: str,
        secret: str,
        version: str,
        target_name: str,
        operator_jwt: str,
        wif_config: _WifConfig | None,
    ) -> tuple[bytes, str]:
        """Build credentials, access the secret version, return ``(bytes, name)``.

        Runs in a thread (``asyncio.to_thread``) because the google-cloud
        client and ``google.auth`` credential refresh — including the WIF STS
        exchange — all perform blocking transport under the hood. Credential
        selection is per-call and already resolved by ``_select_auth_path``:
        *wif_config* present ⇒ the operator-context WIF path (a fresh federated
        credential built from *operator_jwt*, never cached across reads);
        ``None`` ⇒ the SA-direct ADC path (Phase 1, or the #2642 fallback).
        Access-denied, not-found, and transport failures are re-raised as
        :class:`GcpSecretManagerReadError` with an actionable message naming
        project / secret (AC #5) — not a bare ``google.api_core`` exception.
        """
        import google.api_core.exceptions as gcp_exceptions
        from google.cloud import secretmanager

        if wif_config is not None:
            credentials = self._build_wif_credentials(operator_jwt, target_name, wif_config)
        else:
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
                f"non-key-file credential). {_NO_IDENTITY_REMEDY}"
            ) from exc
        if source_credentials is None:
            raise GcpSecretManagerReadError(
                f"Application Default Credentials resolved to no credentials for target "
                f"{target_name!r}; cannot read gsm secrets. {_NO_IDENTITY_REMEDY}"
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

    def _build_wif_credentials(
        self, operator_jwt: str, target_name: str, wif_config: _WifConfig
    ) -> Any:
        """Build a per-operator WIF (external-account) credential (#2232).

        Constructs ``google.auth.identity_pool.Credentials`` that exchange
        *operator_jwt* at the GCP STS (``sts.googleapis.com``) for a
        short-lived federated token against the configured Workload Identity
        Pool + OIDC provider, optionally impersonating
        ``wif_config.service_account`` for the final read. A **fresh**
        credential is built on every call — never stored on the instance — so
        the operator-scoped token is minted per operation and discarded when
        the credential goes out of scope, matching the Vault JIT contract.

        Fails closed with :class:`GcpSecretManagerReadError` when the operator
        JWT is empty (defence in depth behind the shared loader's pre-dispatch
        guard), when the declared pool / provider disagree with the audience
        (a copy-paste misconfiguration), or when google-auth rejects the
        external-account config — never a bare ``google.auth`` exception.
        """
        if not operator_jwt:
            raise GcpSecretManagerReadError(
                f"WIF operator-context read for target {target_name!r} has no operator "
                "JWT to exchange; a system-initiated call cannot federate to GCP "
                "(the empty-raw_jwt guard should have failed this upstream)"
            )
        _assert_wif_audience_consistent(wif_config, target_name)

        service_account = wif_config.service_account
        impersonation_url = (
            _IAM_IMPERSONATION_URL_TEMPLATE.format(service_account=service_account)
            if service_account
            else None
        )

        if self._wif_credentials_factory is not None:
            return self._wif_credentials_factory(
                operator_jwt=operator_jwt,
                wif_config=wif_config,
                service_account_impersonation_url=impersonation_url,
                target_name=target_name,
            )

        from google.auth import exceptions as auth_exceptions
        from google.auth import identity_pool

        supplier = _OperatorJwtSubjectTokenSupplier(operator_jwt)
        try:
            return identity_pool.Credentials(  # type: ignore[no-untyped-call]
                audience=wif_config.audience,
                subject_token_type=wif_config.subject_token_type,
                token_url=_STS_TOKEN_URL,
                subject_token_supplier=supplier,
                service_account_impersonation_url=impersonation_url,
                scopes=[_GCP_CLOUD_PLATFORM_SCOPE],
            )
        except (ValueError, auth_exceptions.GoogleAuthError) as exc:
            raise GcpSecretManagerReadError(
                f"gsm WIF config for target {target_name!r} is invalid: google-auth "
                f"rejected the external-account credential ({type(exc).__name__}); "
                "check GSM_WIF_AUDIENCE, GSM_WIF_SUBJECT_TOKEN_TYPE, and "
                "GSM_WIF_SERVICE_ACCOUNT"
            ) from exc

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
