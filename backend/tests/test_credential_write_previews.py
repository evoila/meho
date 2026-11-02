# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval previews of nine credential writes (#4850).

Each op below is pinned ``credential_write``, so the generic echo of params
is off for it. Its bespoke preview builder must show what changes (which
user, secret, host or patch) and must never show a secret value. Each test
runs the real registered builder through :func:`build_proposed_effect` and
checks both: the fields the approver needs, and that a made-up secret
value appears nowhere in the stored effect.

The builders are registered when each connector package is imported. The
session fixture ``_force_import_connector_modules`` in ``conftest.py``
imports every connector package, so this file does not import them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    describe_preview_provenance,
)

#: A made-up secret. No preview may contain it.
_SECRET = "made-up-secret-Value-4850"


@dataclass
class _FakeDescriptor:
    op_id: str


@dataclass
class _FakeTarget:
    name: str = "t"
    version: str | None = None
    fingerprint: dict[str, Any] | None = None


def _operator() -> Operator:
    return Operator(
        sub="op-4850",
        name="Preview Test Operator",
        email=None,
        raw_jwt="op.4850.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000004850"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _preview(
    op_id: str, params: dict[str, Any], *, target: Any = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the registered builder; return ``(effect, preview)``. Checks no leak."""
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=target,
        params=params,
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None, f"{op_id} has no preview"
    assert effect["op_class"] == "credential_write"
    assert describe_preview_provenance(effect, op_id=op_id) == (True, None)
    assert _SECRET not in json.dumps(effect), f"{op_id} preview shows the secret"
    preview: dict[str, Any] = effect["preview"]
    return effect, preview


@pytest.mark.asyncio
async def test_keycloak_user_reset_password_shows_user_and_vault_path() -> None:
    _, preview = await _preview(
        "keycloak.user.reset_password",
        {
            "username": "alice",
            "realm": "ops",
            "password_secret_ref": "tenants/acme/alice",
            "password_secret_key": "pw",
            "temporary": True,
        },
    )
    assert preview["resource"] == "user_password"
    assert preview["username"] == "alice"
    assert preview["realm"] == "ops"
    assert preview["password_source"] == "vault"
    assert preview["password_secret_ref"] == "tenants/acme/alice"
    assert preview["password_secret_key"] == "pw"
    assert preview["temporary"] is True


@pytest.mark.asyncio
async def test_keycloak_user_reset_password_never_echoes_an_extra_password_param() -> None:
    """Even a stray inline ``password`` param (the schema forbids it) is not shown."""
    _, preview = await _preview(
        "keycloak.user.reset_password",
        {"id": "3f2b8f8e-0000-4000-8000-000000000001", "password": _SECRET},
    )
    assert preview["id"] == "3f2b8f8e-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_vault_userpass_update_password_shows_user_not_password() -> None:
    _, preview = await _preview(
        "vault.auth.userpass.update_password",
        {"username": "svc-backup", "password": _SECRET, "mount": "userpass-prod"},
    )
    assert preview == {
        "resource": "userpass_user",
        "mount": "userpass-prod",
        "username": "svc-backup",
        "sets_password": True,
    }


@pytest.mark.asyncio
async def test_vault_userpass_write_shows_user_and_policies_not_password() -> None:
    _, preview = await _preview(
        "vault.auth.userpass.write",
        {"username": "svc-ci", "password": _SECRET, "token_policies": ["ci-read", "ci-deploy"]},
    )
    assert preview["mount"] == "userpass"
    assert preview["username"] == "svc-ci"
    assert preview["sets_password"] is True
    assert preview["token_policies"] == ["ci-read", "ci-deploy"]
    assert preview["token_policies_change"] == "replace"


@pytest.mark.asyncio
async def test_vault_userpass_write_without_policies_says_unchanged() -> None:
    _, preview = await _preview("vault.auth.userpass.write", {"username": "u", "password": _SECRET})
    assert preview["token_policies_change"] == "unchanged"
    assert "token_policies" not in preview


@pytest.mark.asyncio
async def test_k8s_secret_create_shows_key_names_and_refs_not_values() -> None:
    _, preview = await _preview(
        "k8s.secret.create",
        {
            "name": "db-creds",
            "namespace": "apps",
            "string_data": {"password": _SECRET},
            "data": {"tls.key": _SECRET},
            "string_data_secret_ref": {"api_token": "vault:apps/api#token"},
            "string_data_secret_mount": "kv-apps",
        },
    )
    assert preview["resource"] == "secret"
    assert preview["name"] == "db-creds"
    assert preview["namespace"] == "apps"
    assert preview["type"] == "Opaque"
    assert preview["key_names"] == ["api_token", "password", "tls.key"]
    assert preview["inline_key_names"] == ["password", "tls.key"]
    assert preview["string_data_secret_ref"] == {"api_token": "vault:apps/api#token"}
    assert "data_secret_ref" not in preview
    assert preview["string_data_secret_mount"] == "kv-apps"
    assert "data_secret_mount" not in preview


@pytest.mark.asyncio
async def test_k8s_secret_create_shows_labels_and_annotations() -> None:
    _, preview = await _preview(
        "k8s.secret.create",
        {
            "name": "repo-creds",
            "namespace": "argocd",
            "labels": {"argocd.argoproj.io/secret-type": "repository"},
            "annotations": {"managed-by": "meho"},
            "string_data_secret_ref": {"sshPrivateKey": "vault:argocd/repo#key"},
        },
    )
    assert preview["labels"] == {"argocd.argoproj.io/secret-type": "repository"}
    assert preview["annotations"] == {"managed-by": "meho"}
    assert preview["key_names"] == ["sshPrivateKey"]


@pytest.mark.asyncio
async def test_k8s_secret_create_without_labels_omits_them_from_preview() -> None:
    _, preview = await _preview(
        "k8s.secret.create",
        {"name": "s", "namespace": "apps", "string_data": {"password": _SECRET}},
    )
    assert "labels" not in preview
    assert "annotations" not in preview


@pytest.mark.asyncio
async def test_k8s_job_create_shows_containers_not_env_values_or_command() -> None:
    command = ["/bin/sh", "-c", f"curl -H 'Authorization: {_SECRET}' https://example.invalid"]
    _, preview = await _preview(
        "k8s.job.create",
        {
            "name": "migrate",
            "namespace": "apps",
            "spec": {
                "template": {
                    "spec": {
                        "serviceAccountName": "migrator",
                        "initContainers": [{"name": "wait", "image": "busybox:1.36"}],
                        "containers": [
                            {
                                "name": "run",
                                "image": "registry.example/migrate:2.1",
                                "command": command,
                                "env": [
                                    {"name": "DB_PASSWORD", "value": _SECRET},
                                    {
                                        "name": "API_TOKEN",
                                        "valueFrom": {
                                            "secretKeyRef": {"name": "api", "key": "token"}
                                        },
                                    },
                                ],
                                "envFrom": [
                                    {"secretRef": {"name": "db-creds"}},
                                    {"configMapRef": {"name": "settings"}},
                                ],
                            }
                        ],
                    }
                }
            },
        },
    )
    assert preview["resource"] == "job"
    assert preview["name"] == "migrate"
    assert preview["namespace"] == "apps"
    assert preview["service_account_name"] == "migrator"
    assert preview["init_containers"] == [
        {"name": "wait", "image": "busybox:1.36", "env_var_names": [], "env_from": []}
    ]
    (container,) = preview["containers"]
    assert container["name"] == "run"
    assert container["image"] == "registry.example/migrate:2.1"
    assert container["env_var_names"] == ["API_TOKEN", "DB_PASSWORD"]
    assert container["env_from"] == ["secret:db-creds", "config_map:settings"]
    assert container["command_bytes"] == sum(len(part.encode()) for part in command)
