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


def _job_params(container: dict[str, Any], **pod_fields: Any) -> dict[str, Any]:
    """``k8s.job.create`` params for one container and the given pod fields."""
    return {
        "name": "node-check",
        "namespace": "default",
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {"restartPolicy": "Never", "containers": [container], **pod_fields}
            },
        },
    }


@pytest.mark.asyncio
async def test_k8s_job_create_shows_privileges_node_and_command_not_secrets() -> None:
    """The approver sees what a privileged Job runs and where (#5259)."""
    command = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "uptime"]
    effect, preview = await _preview(
        "k8s.job.create",
        _job_params(
            {
                "name": "check",
                "image": "busybox:1.36",
                "command": command,
                "args": [f"--token={_SECRET}", "--verbose"],
                "securityContext": {"privileged": True},
                "env": [
                    {"name": "API_TOKEN", "value": _SECRET},
                    {
                        "name": "DB_PASSWORD",
                        "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}},
                    },
                ],
                "envFrom": [{"configMapRef": {"name": "settings"}}],
                "volumeMounts": [{"name": "host-root", "mountPath": "/host"}],
            },
            nodeName="node-01",
            hostPID=True,
            serviceAccountName="checker",
            volumes=[{"name": "host-root", "hostPath": {"path": "/"}}],
        ),
    )
    assert preview["resource"] == "job"
    assert preview["name"] == "node-check"
    assert preview["namespace"] == "default"
    spec = preview["spec"]
    assert spec["backoffLimit"] == 0
    pod = spec["template"]["spec"]
    assert pod["nodeName"] == "node-01"
    assert pod["hostPID"] is True
    assert pod["serviceAccountName"] == "checker"
    assert pod["volumes"] == [{"name": "host-root", "hostPath": {"path": "/"}}]
    (container,) = pod["containers"]
    assert container["image"] == "busybox:1.36"
    assert container["command"] == command
    assert container["args"][1] == "--verbose"
    assert container["securityContext"] == {"privileged": True}
    assert container["volumeMounts"] == [{"name": "host-root", "mountPath": "/host"}]
    assert container["env"] == [
        {"name": "API_TOKEN", "value": "***REDACTED***"},
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}},
    ]
    assert container["envFrom"] == [{"configMapRef": {"name": "settings"}}]
    text = json.dumps(effect)
    assert "privileged" in text
    assert "nsenter" in text


@pytest.mark.asyncio
async def test_k8s_job_create_hides_secret_shapes_anywhere_in_the_spec() -> None:
    """Secret-shaped text and probe header values are hidden at any depth."""
    effect, preview = await _preview(
        "k8s.job.create",
        _job_params(
            {
                "name": "run",
                "image": "registry.example/migrate:2.1",
                "command": ["/bin/sh", "-c", f"curl -H 'Authorization: Bearer {_SECRET}' x"],
                "args": [f"API_KEY={_SECRET}"],
                "livenessProbe": {
                    "httpGet": {
                        "path": "/health",
                        "port": 8080,
                        "httpHeaders": [{"name": "X-Auth", "value": _SECRET}],
                    }
                },
            },
            initContainers=[
                {"name": "wait", "image": "busybox:1.36", "env": [{"name": "K", "value": _SECRET}]}
            ],
        ),
    )
    pod = preview["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["command"][:2] == ["/bin/sh", "-c"]
    assert container["livenessProbe"]["httpGet"]["httpHeaders"] == [
        {"name": "X-Auth", "value": "***REDACTED***"}
    ]
    assert pod["initContainers"][0]["env"] == [{"name": "K", "value": "***REDACTED***"}]
    assert "made-up-secret" not in json.dumps(effect)


#: A short made-up password: short values must be hidden too.
_SHORT = "Xq9"
#: A URL with a user and the short made-up password in it.
_URL_WITH_PASSWORD = f"https://bob:{_SHORT}@example.invalid"  # trufflehog:ignore


@pytest.mark.parametrize(
    ("args", "kept"),
    [
        (["-c", f"export PGPASSWORD={_SECRET} && pg_dump -h db app"], "pg_dump"),
        (["-c", f"MYSQL_ROOT_PASSWORD='{_SECRET} {_SHORT}' mysqld"], "mysqld"),
        ([f"AWS_SECRET_ACCESS_KEY={_SECRET}", f"API_KEY={_SHORT}"], "AWS_SECRET_ACCESS_KEY"),
        ([f"--password={_SHORT}", f"--token={_SHORT}", "--dry-run"], "--dry-run"),
        (["--password", _SHORT, "--verbose"], "--verbose"),
        (["--password", f"{_SHORT} phrase", "--verbose"], "--verbose"),
        (["-c", f"mytool --password '{_SHORT} phrase' run"], "run"),
        (["-c", f'mytool --password "{_SHORT} phrase" run'], "run"),
        (["-c", f'mytool --password "{_SHORT}\\" phrase" run'], "run"),
        (["-c", f"mytool --password {_SHORT}\\ phrase run"], "run"),
        (["-c", f"mytool --password={_SHORT}\\ phrase run"], "run"),
        (["-c", f"PGPASSWORD='{_SHORT} phrase pg_dump"], "PGPASSWORD="),
        (["-c", f'env "PGPASSWORD={_SHORT}" pg_dump app'], "pg_dump app"),
        (["-c", f"mysql -u root -p {_SHORT} app"], "mysql"),
        (["-c", f"curl -u admin:{_SHORT} https://example.invalid"], "admin:"),  # gitleaks:allow
        (["curl", "--user", f"admin:{_SHORT}", _URL_WITH_PASSWORD], "curl"),
        (["-c", f"git clone https://ghp_{_SECRET.replace('-', '')}@example.invalid/r"], "clone"),
    ],
)
@pytest.mark.asyncio
async def test_k8s_job_create_hides_secret_values_on_the_command_line(
    args: list[str], kept: str
) -> None:
    """A secret value on the command line is hidden; the command words stay."""
    effect, preview = await _preview(
        "k8s.job.create",
        _job_params({"name": "run", "image": "busybox:1.36", "command": ["sh"], "args": args}),
    )
    text = json.dumps(effect)
    assert _SHORT not in text
    assert _SECRET.replace("-", "") not in text
    assert kept in text
    (container,) = preview["spec"]["template"]["spec"]["containers"]
    assert len(container["args"]) == len(args)


@pytest.mark.asyncio
async def test_k8s_job_create_hides_the_whole_argument_after_a_secret_flag() -> None:
    """A password with a space in it is hidden whole, as a list item or quoted."""
    _, preview = await _preview(
        "k8s.job.create",
        _job_params(
            {
                "name": "run",
                "image": "busybox:1.36",
                "command": ["sh"],
                "args": ["--password", f"{_SHORT} phrase", "-c", f"tool -p '{_SHORT} phrase' x"],
            }
        ),
    )
    (container,) = preview["spec"]["template"]["spec"]["containers"]
    assert container["args"] == ["--password", "***REDACTED***", "-c", "tool -p ***REDACTED*** x"]


@pytest.mark.asyncio
async def test_k8s_job_create_hides_a_password_with_shell_escapes_whole() -> None:
    """An escaped quote or space is part of the password, not its end."""
    _, preview = await _preview(
        "k8s.job.create",
        _job_params(
            {
                "name": "run",
                "image": "busybox:1.36",
                "command": ["sh", "-c"],
                "args": [
                    f'tool --password "{_SHORT}\\" phrase" run',
                    f"tool --password {_SHORT}\\ phrase run",
                    f"tool -p '{_SHORT}' --user admin:{_SHORT}\\ phrase run",  # gitleaks:allow
                ],
            }
        ),
    )
    (container,) = preview["spec"]["template"]["spec"]["containers"]
    assert container["args"] == [
        "tool --password ***REDACTED*** run",
        "tool --password ***REDACTED*** run",
        "tool -p ***REDACTED*** --user admin:***REDACTED*** run",
    ]


@pytest.mark.asyncio
async def test_k8s_job_create_keeps_the_commands_after_a_quoted_assignment() -> None:
    """A value inside a quoted part ends at the closing quote, not at the string's end."""
    _, preview = await _preview(
        "k8s.job.create",
        _job_params(
            {
                "name": "run",
                "image": "busybox:1.36",
                "command": ["sh", "-c"],
                "args": [
                    f'env "PGPASSWORD={_SHORT}" pg_dump app',
                    f"env 'PGPASSWORD={_SHORT} phrase' pg_dump app",
                    f'sh -c "pg_dump --password={_SHORT} app" && echo done',
                    f"sh -c 'curl --user=admin:{_SHORT} x.invalid' && echo done",  # gitleaks:allow
                ],
            }
        ),
    )
    (container,) = preview["spec"]["template"]["spec"]["containers"]
    assert _SHORT not in json.dumps(container)
    assert "phrase" not in json.dumps(container)
    assert container["args"][:2] == [
        'env "PGPASSWORD=***REDACTED***" pg_dump app',
        "env 'PGPASSWORD=***REDACTED***' pg_dump app",
    ]
    assert container["args"][2].endswith(' app" && echo done')
    assert container["args"][3] == (
        "sh -c 'curl --user=admin:***REDACTED*** x.invalid' && echo done"  # gitleaks:allow
    )


@pytest.mark.asyncio
async def test_k8s_job_create_reads_command_and_args_as_one_command_line() -> None:
    """A secret flag that ends ``command`` hides the first item of ``args``."""
    _, preview = await _preview(
        "k8s.job.create",
        _job_params(
            {
                "name": "run",
                "image": "busybox:1.36",
                "command": ["tool", "--password"],
                "args": [_SHORT, "--user", f"admin:{_SHORT}", "--verbose"],  # gitleaks:allow
            }
        ),
    )
    (container,) = preview["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["tool", "--password"]
    assert container["args"] == ["***REDACTED***", "--user", "admin:***REDACTED***", "--verbose"]


@pytest.mark.asyncio
async def test_k8s_job_create_keeps_plain_flags_on_the_command_line() -> None:
    """Flags that carry no secret are shown as they are."""
    command = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "ps", "-u", "root"]
    _, preview = await _preview(
        "k8s.job.create",
        _job_params({"name": "run", "image": "busybox:1.36", "command": command}),
    )
    (container,) = preview["spec"]["template"]["spec"]["containers"]
    assert container["command"] == command


@pytest.mark.parametrize(
    ("op_id", "action"),
    [("sddc.host.validate", "validate"), ("sddc.host.commission", "commission")],
)
@pytest.mark.asyncio
async def test_sddc_host_ops_show_hosts_not_passwords(op_id: str, action: str) -> None:
    host = {
        "fqdn": "esxi-01.example.invalid",
        "username": "root",
        "password": _SECRET,
        "storageType": "NFS",
        "networkPoolId": "pool-1",
        "extraSecret": _SECRET,
    }
    second = {**host, "fqdn": "esxi-02.example.invalid"}
    _, preview = await _preview(op_id, {"spec": [host, second]})
    assert preview["resource"] == "esxi_hosts"
    assert preview["action"] == action
    assert preview["host_count"] == 2
    assert preview["hosts"][0] == {
        "fqdn": "esxi-01.example.invalid",
        "username": "root",
        "storageType": "NFS",
        "networkPoolId": "pool-1",
    }
    assert preview["hosts"][1]["fqdn"] == "esxi-02.example.invalid"


@pytest.mark.parametrize(("version", "verify_only"), [("9.0.2.0", False), ("9.1.0.0", True)])
@pytest.mark.asyncio
async def test_holodeck_router_patch_shows_patches_and_mode(
    version: str, verify_only: bool
) -> None:
    _, preview = await _preview("holodeck.router.patch", {}, target=_FakeTarget(version=version))
    assert preview["resource"] == "holorouter_modules"
    assert preview["hr_version"] == version
    assert preview["verify_only"] is verify_only
    assert [patch["patch_id"] for patch in preview["patches"]] == ["C1", "C2", "C3"]
    assert all(patch["file_path"].endswith(".psm1") for patch in preview["patches"])


@pytest.mark.asyncio
async def test_holodeck_router_patch_without_target_version() -> None:
    _, preview = await _preview("holodeck.router.patch", {"stray": _SECRET})
    assert preview["hr_version"] is None
    assert preview["verify_only"] is False


@pytest.mark.asyncio
async def test_vmware_guest_file_read_shows_file_and_login_ref() -> None:
    _, preview = await _preview(
        "vmware.composite.vm.guest.file.read",
        {
            "vm": "vm-42",
            "guest_path": "/etc/app/config.yaml",
            "fetch_content": True,
            "max_inline_bytes": 4096,
            "guest_secret_ref": "targets/app-vm-01",
            "password": _SECRET,
        },
    )
    assert preview == {
        "vm": "vm-42",
        "guest_path": "/etc/app/config.yaml",
        "fetch_content": True,
        "max_inline_bytes": 4096,
        "guest_secret_ref": "targets/app-vm-01",
    }


@pytest.mark.asyncio
async def test_vmware_guest_file_read_metadata_only() -> None:
    _, preview = await _preview(
        "vmware.composite.vm.guest.file.read",
        {"vm": "vm-42", "guest_path": "/etc/os-release", "max_inline_bytes": 4096},
    )
    assert preview == {"vm": "vm-42", "guest_path": "/etc/os-release", "fetch_content": False}
