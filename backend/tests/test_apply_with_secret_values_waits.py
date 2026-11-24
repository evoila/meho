# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A ``k8s.apply`` whose manifest holds a secret-looking value is a credential call.

An object holds a secret-looking value when a key of its ``data`` or
``stringData`` has a secret name, an ``env`` entry has a secret name and a
value, or a text has a secret shape. Such an apply waits for a person for
every caller, with or without a standing grant, an agent grant or a run
approval. Its stored request keeps the manifest only in the encrypted
handoff, also when it is approved and never runs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import op_classes
from meho_backplane.connectors.kubernetes.op_classes import manifest_holds_credential
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus, PermissionVerdict
from meho_backplane.operations._validate import CREDENTIAL_WAIT_NOTE, compute_params_hash
from meho_backplane.operations.approval_handoff import ApprovalExecutionPayload
from meho_backplane.operations.approval_queue import create_pending_request, get_request
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.settings import get_settings
from tests.test_credential_calls_always_wait import (
    _TENANT,
    _agent,
    _name_in_every_grant,
    _register_all,
    _service,
)
from tests.test_credential_capable_calls_wait import _gate, _row

_PW = "made-up-pw-value"

_CONFIG_MAP_WITH_A_SECRET_NAME = f"""
apiVersion: v1
kind: ConfigMap
metadata: {{name: web-settings, namespace: apps}}
data:
  mode: fast
  password: {_PW}
"""
#: A database address with a made-up login in it.
_DB_ADDRESS = "postgres://app:" + _PW + "@db.invalid:5432/app"  # trufflehog:ignore
_ENV_WITH_A_SECRET_SHAPE = f"""
apiVersion: apps/v1
kind: Deployment
metadata: {{name: web, namespace: apps}}
spec:
  template:
    spec:
      containers:
        - name: web
          image: example/web:1
          env:
            - {{name: LOG_LEVEL, value: debug}}
            - {{name: DB_URL, value: "{_DB_ADDRESS}"}}
"""
_ENV_WITH_A_SECRET_NAME = f"""
apiVersion: batch/v1
kind: CronJob
metadata: {{name: nightly, namespace: apps}}
spec:
  jobTemplate:
    spec:
      template:
        spec:
          initContainers:
            - name: init
              image: example/init:1
              env: [{{name: API_TOKEN, value: {_PW}}}]
"""
_STRING_DATA_IN_A_LIST = f"""
apiVersion: v1
kind: List
items:
  - apiVersion: example.com/v1
    kind: Settings
    metadata: {{name: a}}
    stringData: {{client_secret: {_PW}}}
"""
_BINARY_DATA_WITH_A_SECRET_NAME = """
apiVersion: v1
kind: ConfigMap
metadata: {name: web-files, namespace: apps}
binaryData: {api_token: bWFkZS11cC12YWx1ZQ==}
"""
_ENV_FROM_A_REFERENCE = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: apps}
spec:
  template:
    spec:
      containers:
        - name: web
          image: example/web:1
          env:
            - name: DB_PASSWORD
              valueFrom: {secretKeyRef: {name: db-login, key: password}}
            - {name: API_TOKEN, value: ""}
            - {name: LOG_LEVEL, value: debug}
"""
_ENV_AT_THE_TOP = """
apiVersion: example.invalid/v1
kind: AppSettings
metadata: {name: web, namespace: apps}
env:
  - {name: DB_PASSWORD, value: plain-text-value}
"""
_PLAIN_CONFIG_MAP = """
apiVersion: v1
kind: ConfigMap
metadata: {name: web-settings, namespace: apps}
data: {mode: fast, url: "https://db.invalid/app"}
"""

_SECRET_LOOKING = {
    "config-map-secret-name": _CONFIG_MAP_WITH_A_SECRET_NAME,
    "env-secret-shape": _ENV_WITH_A_SECRET_SHAPE,
    "binary-data-secret-name": _BINARY_DATA_WITH_A_SECRET_NAME,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _person() -> Operator:
    return Operator(
        sub="op:person",
        name="Person",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


# ---------------------------------------------------------------------------
# The value check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest",
    [
        _CONFIG_MAP_WITH_A_SECRET_NAME,
        _ENV_WITH_A_SECRET_SHAPE,
        _ENV_WITH_A_SECRET_NAME,
        _STRING_DATA_IN_A_LIST,
        _PLAIN_CONFIG_MAP + "---\n" + _CONFIG_MAP_WITH_A_SECRET_NAME,
        _ENV_AT_THE_TOP,
        _BINARY_DATA_WITH_A_SECRET_NAME,
    ],
    ids=[
        "data-key",
        "env-shape",
        "env-name",
        "list-item",
        "second-document",
        "env-at-the-top",
        "binary-data-key",
    ],
)
def test_a_manifest_with_a_secret_looking_value_counts(manifest: str) -> None:
    assert manifest_holds_credential({"manifest": manifest}) is True


@pytest.mark.parametrize(
    "manifest",
    [_PLAIN_CONFIG_MAP, _ENV_FROM_A_REFERENCE],
    ids=["plain-config-map", "env-from-a-reference"],
)
def test_a_manifest_without_a_secret_looking_value_does_not_count(manifest: str) -> None:
    assert manifest_holds_credential({"manifest": manifest}) is False


def test_the_value_check_reads_a_shared_value_once() -> None:
    """Many objects that name one large value through an alias stay fast."""
    shared = ", ".join(f"v{i}" for i in range(20_000))
    items = "\n".join("  - {kind: Settings, spec: *big}" for _ in range(5_000))
    manifest = f"apiVersion: v1\nkind: List\nshared: &big [{shared}]\nitems:\n{items}\n"

    started = time.perf_counter()
    assert manifest_holds_credential({"manifest": manifest}) is False
    assert time.perf_counter() - started < 30


def _count_name_checks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checked: list[str] = []
    real_check = op_classes.is_secret_param_name

    def counting_check(name: str) -> bool:
        checked.append(name)
        return real_check(name)

    monkeypatch.setattr(op_classes, "is_secret_param_name", counting_check)
    return checked


def test_the_value_check_reads_the_names_of_a_shared_env_list_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many containers that name one long env list through an alias read its names once."""
    checked = _count_name_checks(monkeypatch)
    entries = ", ".join(f"{{name: SETTING_{i}, value: v{i}}}" for i in range(1_000))
    containers = "\n".join(f"    - {{name: c{i}, env: *env}}" for i in range(200))
    manifest = (
        f"apiVersion: v1\nkind: Pod\nmetadata: {{name: web}}\n"
        f"spec:\n  shared: &env [{entries}]\n  containers:\n{containers}\n"
    )

    assert manifest_holds_credential({"manifest": manifest}) is False
    # Two keys for each of the 1,000 entries, once; not once for each container.
    assert len(checked) <= 3_000


def test_the_value_check_reads_the_keys_of_a_shared_data_map_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many list items that name one large data map through an alias read its keys once."""
    checked = _count_name_checks(monkeypatch)
    keys = ", ".join(f"setting_{i}: v{i}" for i in range(1_000))
    items = "\n".join(
        f"  - {{kind: ConfigMap, metadata: {{name: c{i}}}, data: *data}}" for i in range(200)
    )
    manifest = f"apiVersion: v1\nkind: List\nshared: &data {{{keys}}}\nitems:\n{items}\n"

    assert manifest_holds_credential({"manifest": manifest}) is False
    # The 1,000 keys are read as a data map once and as a map once.
    assert len(checked) <= 3_000


# ---------------------------------------------------------------------------
# Such an apply waits for a person
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [False, True], ids=["no-grant", "grant-and-run-approval"])
@pytest.mark.parametrize(
    "operator",
    [_person(), _agent(), _service()],
    ids=["person", "agent", "service"],
)
@pytest.mark.parametrize("manifest", list(_SECRET_LOOKING.values()), ids=list(_SECRET_LOOKING))
async def test_an_apply_with_a_secret_looking_value_waits_for_a_person(
    manifest: str, operator: Operator, granted: bool
) -> None:
    rows = await _register_all()
    apply_row = _row(rows, "k8s.apply")
    if granted:
        await _name_in_every_grant([apply_row])

    today, reason, shadow = await _gate(operator, apply_row, {"manifest": manifest})

    assert today is PermissionVerdict.NEEDS_APPROVAL, reason
    assert shadow is Verdict.WAIT, reason
    assert "auto-granted" not in reason
    if operator.principal_kind is PrincipalKind.SERVICE:
        assert reason.endswith(CREDENTIAL_WAIT_NOTE), reason


# ---------------------------------------------------------------------------
# The stored request keeps the manifest only in the encrypted handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True], ids=["waiting", "approved-never-run"])
@pytest.mark.parametrize("manifest", list(_SECRET_LOOKING.values()), ids=list(_SECRET_LOOKING))
async def test_the_stored_request_keeps_the_manifest_only_in_the_handoff(
    manifest: str, approved: bool
) -> None:
    params: dict[str, Any] = {"manifest": manifest}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_service(),
            connector_id="kubernetes-1.x",
            op_id="k8s.apply",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        if approved:
            request.status = ApprovalRequestStatus.APPROVED.value
            request.reviewed_by = "op:person"
            request.decided_at = datetime.now(UTC)
        await s.commit()
        request_id = request.id

    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT, request_id=request_id)
        handoffs = (
            await s.scalars(
                select(ApprovalExecutionPayload).where(
                    ApprovalExecutionPayload.request_id == request_id
                )
            )
        ).all()
    assert row.params is None
    assert row.execution_handle is not None
    assert len(handoffs) == 1
    assert _PW not in json.dumps({"params": row.params, "parent": row.resume_parent})
