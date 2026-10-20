# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make every Secret the built-in core needs, if it is missing.

The chart runs this module as the ``secrets`` Helm hook Job
(``deploy/charts/meho/templates/core-secrets-job.yaml``) before every install
and upgrade. The chart renders a plan into ``MEHO_CORE_SECRETS_PLAN``:

* ``make``: the ``<release>-core-…`` Secrets this Job makes when they are
  missing, each with a ``kind`` that fixes its keys and their format;
* ``check``: Secrets the operator named in values instead, with the keys
  MEHO reads from them.

Rules (docs/codebase/core-secrets.md):

* An existing Secret wins. Each Secret is read once. "Not found" leads to one
  create. "Already exists" (HTTP 409) on that create means kept.
* Every read happens before the first create. Any read error other than
  "not found" stops the Job, so nothing is created.
* A Secret the install uses must hold every key MEHO reads from it. A missing
  key stops the Job with one sentence that names the Secret and the key.
* The output names Secrets and ``created``, ``kept`` or ``checked``. It never
  holds a value.

Exit code ``0`` on success, ``1`` on any failure, with the reason on stderr as
``core_secrets_failed: <sentence>``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import string
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, assert_never
from urllib.parse import quote

from cryptography.fernet import Fernet
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from meho_backplane.core.tls import self_signed_tls

__all__ = [
    "MANAGED_BY_LABELS",
    "NAMESPACE_ENV",
    "PLAN_ENV",
    "CoreSecretsError",
    "KubernetesSecretStore",
    "Plan",
    "SecretStore",
    "StoredSecret",
    "build_data",
    "load_plan",
    "main",
    "reconcile",
]

PLAN_ENV = "MEHO_CORE_SECRETS_PLAN"
NAMESPACE_ENV = "POD_NAMESPACE"
# The only label on a made Secret. No chart labels, so Argo CD label tracking
# can never adopt, diff or prune it.
MANAGED_BY_LABELS: Mapping[str, str] = {"meho.evoila.com/managed-by": "core-bootstrap"}

# One alphabet for every password: no `$` for Keycloak to expand, nothing to
# escape in JSON or in a database URL.
_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LENGTH = 32
# Seconds per Kubernetes API call. A hung call fails the attempt, and the
# Job's backoffLimit decides whether it runs again.
_API_TIMEOUT_S = 30

Kind = Literal[
    "postgres-superuser",
    "postgres-meho",
    "postgres-keycloak",
    "keycloak-bootstrap",
    "keycloak-admin",
    "ui-auth",
    "check-runner",
    "approval-handoff",
    "broadcast-auth",
    "tls",
]


@dataclass(frozen=True)
class _KindSpec:
    keys: tuple[str, ...]
    secret_type: str = "Opaque"
    needs_host: bool = False


_KINDS: Mapping[str, _KindSpec] = {
    "postgres-superuser": _KindSpec(("password",)),
    "postgres-meho": _KindSpec(("password", "url"), needs_host=True),
    "postgres-keycloak": _KindSpec(("password",)),
    "keycloak-bootstrap": _KindSpec(("client_id", "client_secret")),
    "keycloak-admin": _KindSpec(("client_secret",)),
    "ui-auth": _KindSpec(("session_encryption_key", "client_secret")),
    "check-runner": _KindSpec(("client_secret",)),
    "approval-handoff": _KindSpec(("approval_handoff_encryption_key",)),
    "broadcast-auth": _KindSpec(("requirepass",)),
    "tls": _KindSpec(("tls.crt", "tls.key", "ca.crt"), "kubernetes.io/tls", needs_host=True),
}


@dataclass(frozen=True)
class StoredSecret:
    """A Secret's decoded values and its annotations."""

    data: Mapping[str, str]
    annotations: Mapping[str, str]


class CoreSecretsError(Exception):
    """A failure the Job reports as one plain sentence before it exits 1."""


class MakeEntry(BaseModel):
    """A ``<release>-core-…`` Secret the Job makes when it is missing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    kind: Kind
    # The database host for ``postgres-meho``, the Ingress host for ``tls``.
    host: str | None = None

    @model_validator(mode="after")
    def _host_when_needed(self) -> MakeEntry:
        if _KINDS[self.kind].needs_host and not self.host:
            raise ValueError(f"kind {self.kind} needs a host")
        return self


class CheckEntry(BaseModel):
    """A Secret the operator named in values; the Job only checks its keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    values_key: str = Field(min_length=1)
    keys: tuple[str, ...] = Field(min_length=1)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    make: tuple[MakeEntry, ...] = ()
    check: tuple[CheckEntry, ...] = ()


class SecretStore(Protocol):
    """The two Secret calls the Job needs, in one namespace."""

    async def read_keys(self, name: str) -> frozenset[str] | None:
        """Return the Secret's key names, or ``None`` if it does not exist.

        Any other failure raises :class:`CoreSecretsError`.
        """
        ...

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        """Create the Secret: ``True`` if created, ``False`` if it already existed.

        Any other failure raises :class:`CoreSecretsError`.
        """
        ...


def _new_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_PASSWORD_LENGTH))


def _new_fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


def build_data(entry: MakeEntry) -> dict[str, str]:
    """Return fresh values for every key of ``entry.kind``."""
    host = entry.host or ""
    match entry.kind:
        case "postgres-superuser" | "postgres-keycloak":
            return {"password": _new_password()}
        case "postgres-meho":
            password = _new_password()
            url = f"postgresql+asyncpg://meho:{quote(password, safe='')}@{host}:5432/meho"
            return {"password": password, "url": url}
        case "keycloak-bootstrap":
            return {"client_id": _new_password(), "client_secret": _new_password()}
        case "keycloak-admin" | "check-runner":
            return {"client_secret": _new_password()}
        case "ui-auth":
            return {"session_encryption_key": _new_fernet_key(), "client_secret": _new_password()}
        case "approval-handoff":
            return {"approval_handoff_encryption_key": _new_fernet_key()}
        case "broadcast-auth":
            return {"requirepass": _new_password()}
        case "tls":
            return self_signed_tls(host)
        case _:
            assert_never(entry.kind)


def _require_keys(name: str, present: frozenset[str], required: tuple[str, ...], fix: str) -> None:
    for key in required:
        if key not in present:
            raise CoreSecretsError(f'Secret "{name}" has no key "{key}"; {fix}.')


def _say(name: str, result: str) -> None:
    print(f"secret {name}: {result}", flush=True)


async def reconcile(plan: Plan, store: SecretStore) -> None:
    """Check the named Secrets, then make the missing core Secrets."""
    missing: list[MakeEntry] = []
    for check in plan.check:
        present = await store.read_keys(check.name)
        clear = f"clear {check.values_key} so the install makes its own Secret"
        if present is None:
            raise CoreSecretsError(
                f'Secret "{check.name}" (named in {check.values_key}) does not exist; '
                f"create it, or {clear}."
            )
        _require_keys(check.name, present, check.keys, f"add the key, or {clear}")
    for entry in plan.make:
        present = await store.read_keys(entry.name)
        if present is None:
            missing.append(entry)
            continue
        _require_keys(
            entry.name,
            present,
            _KINDS[entry.kind].keys,
            "add the key, or delete the Secret so the install makes a new one",
        )

    for check in plan.check:
        _say(check.name, f"checked (named in {check.values_key})")
    for entry in plan.make:
        if entry not in missing:
            _say(entry.name, "kept")
    for entry in missing:
        created = await store.create(entry.name, build_data(entry), _KINDS[entry.kind].secret_type)
        _say(entry.name, "created" if created else "kept")


def _describe(exc: ApiException) -> str:
    """Status line plus the API's own message; never headers or the raw body."""
    message = ""
    try:
        body = json.loads(exc.body) if exc.body else None
    except (TypeError, ValueError):
        body = None
    if isinstance(body, dict):
        message = str(body.get("message") or "")
    status = f"HTTP {exc.status} {exc.reason}"
    return f"{status}: {message}" if message else status


class KubernetesSecretStore:
    """:class:`SecretStore` over the Kubernetes API (``CoreV1Api``)."""

    def __init__(self, api: Any, namespace: str) -> None:
        self._api = api
        self._namespace = namespace

    async def read_keys(self, name: str) -> frozenset[str] | None:
        try:
            secret = await self._api.read_namespaced_secret(
                name, self._namespace, _request_timeout=_API_TIMEOUT_S
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise CoreSecretsError(f'cannot read Secret "{name}": {_describe(exc)}') from None
        return frozenset(secret.data or {})

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        body = client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=client.V1ObjectMeta(name=name, labels=dict(MANAGED_BY_LABELS)),
            type=secret_type,
            string_data=dict(data),
        )
        try:
            await self._api.create_namespaced_secret(
                self._namespace, body, _request_timeout=_API_TIMEOUT_S
            )
        except ApiException as exc:
            if exc.status == 409:
                return False
            raise CoreSecretsError(f'cannot create Secret "{name}": {_describe(exc)}') from None
        return True

    async def read_data(self, name: str) -> StoredSecret | None:
        """Return the Secret's values and annotations, or ``None`` if it does not exist."""
        try:
            secret = await self._api.read_namespaced_secret(
                name, self._namespace, _request_timeout=_API_TIMEOUT_S
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise CoreSecretsError(f'cannot read Secret "{name}": {_describe(exc)}') from None
        data = {key: base64.b64decode(value).decode() for key, value in (secret.data or {}).items()}
        return StoredSecret(data=data, annotations=dict(secret.metadata.annotations or {}))

    async def annotate(self, name: str, annotations: Mapping[str, str]) -> None:
        """Merge *annotations* into the Secret's metadata; values stay as they are."""
        body = {"metadata": {"annotations": dict(annotations)}}
        try:
            await self._api.patch_namespaced_secret(
                name, self._namespace, body, _request_timeout=_API_TIMEOUT_S
            )
        except ApiException as exc:
            raise CoreSecretsError(f'cannot annotate Secret "{name}": {_describe(exc)}') from None


def load_plan(environ: Mapping[str, str]) -> Plan:
    raw = environ.get(PLAN_ENV)
    if not raw:
        raise CoreSecretsError(f"{PLAN_ENV} is not set; the chart sets it on the Job.")
    try:
        return Plan.model_validate_json(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise CoreSecretsError(f"{PLAN_ENV} is not a valid plan: {problems}") from None


async def _run_in_cluster(plan: Plan, namespace: str) -> None:
    # kubernetes_asyncio ships no type hints (see the mypy override in pyproject.toml).
    config.load_incluster_config()  # type: ignore[no-untyped-call]
    async with client.ApiClient() as api_client:
        await reconcile(plan, KubernetesSecretStore(client.CoreV1Api(api_client), namespace))


def main() -> int:
    try:
        plan = load_plan(os.environ)
        namespace = os.environ.get(NAMESPACE_ENV)
        if not namespace:
            raise CoreSecretsError(f"{NAMESPACE_ENV} is not set; the chart sets it on the Job.")
        asyncio.run(_run_in_cluster(plan, namespace))
    except CoreSecretsError as exc:
        print(f"core_secrets_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"core_secrets_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
