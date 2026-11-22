# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every write to a sub-part of a Kubernetes object waits for a person, except status and scale.

A ``POST``, ``PUT``, ``PATCH`` or ``DELETE`` through an ingested Kubernetes
API file on a path that goes on past the name of an object waits for a
person on every call: for example a pod's ``eviction``, a Namespace's
``finalize`` or the action of an add-on API. No standing grant, no agent
grant and no run approval covers it, also not as a step. Only ``status``
and ``scale`` right after the name of the object keep the rules of their
operation. Reads keep their rules.
"""

from __future__ import annotations

import pytest

from meho_backplane import op_classes
from meho_backplane.broadcast.events import classify_op, resolve_op_class
from meho_backplane.connectors.kubernetes.ingested_paths import ingested_op_is_credential_call
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PermissionVerdict
from meho_backplane.operations.service_grants import credential_grant_reason
from tests.test_credential_calls_always_wait import _name_in_every_grant, _service
from tests.test_every_k8s_apply_and_api_write_waits import (
    _ADD_ON_ACTION,
    _EVICTION,
    _FINALIZE,
    _PREFIX,
    _PREFIXES,
    _assert_waits_for_every_login,
    _store,
)
from tests.test_guest_reads_and_pod_applies_wait import _ingested, _settings_env, _verdict

__all__ = ["_settings_env"]

#: ``(method, path)`` of sub-part writes, each without a path prefix.
_SUBRESOURCE_WRITES: list[tuple[str, str]] = [
    ("POST", "/api/v1/namespaces/{namespace}/pods/{name}/eviction"),
    ("POST", "/api/v1/namespaces/{namespace}/pods/{name}/binding"),
    ("PATCH", "/api/v1/namespaces/{namespace}/pods/{name}/resize"),
    ("PUT", "/api/v1/namespaces/{name}/finalize"),
    ("PUT", "/api/v1/nodes/{name}/made-up-part"),
    ("PUT", "/apis/widgets.example.test/v1/namespaces/{namespace}/widgets/{name}/start"),
    ("POST", "/apis/widgets.example.test/v1/widgets/{name}/restart"),
    ("PUT", "/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale/extra"),
    ("PUT", "/api/v1/namespaces/{namespace}/pods/{name}/status/extra"),
]


def _variants() -> list[str]:
    """Each sub-part write with and without the prefix, in lower case, and with a trailing ``/``."""
    return [
        op_id
        for method, path in _SUBRESOURCE_WRITES
        for prefix in _PREFIXES
        for op_id in (
            f"{method}:{prefix}{path}",
            f"{method.lower()}:{prefix}{path}",
            f"{method}:{prefix}{path}/",
        )
    ]


@pytest.mark.parametrize("op_id", _variants())
def test_every_subresource_write_waits(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True
    assert credential_grant_reason(op_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES, ids=["plain", "prefix"])
@pytest.mark.parametrize("op_id", [_EVICTION, _FINALIZE, _ADD_ON_ACTION])
async def test_a_subresource_write_waits_for_every_login(op_id: str, prefix: str) -> None:
    method, path = op_id.split(":", 1)
    row = await _store(f"{method}:{prefix}{path}")
    params = {"namespace": "apps", "name": "a", "cluster": "c-1"}
    await _assert_waits_for_every_login(row, params)


#: ``status`` and ``scale`` right after the name of an object keep their rules.
_KEPT_WRITES: list[str] = [
    "PUT:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/status",
    "PATCH:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
    "PUT:/apis/helm.cattle.io/v1/namespaces/{namespace}/helmcharts/{name}/status",
    "PATCH:/api/v1/namespaces/{name}/status",
    "PUT:/api/v1/nodes/{name}/status",
    "PUT:/api/v1/namespaces/{namespace}/replicationcontrollers/{name}/scale",
    "PUT:/apis/widgets.example.test/v1/widgets/{name}/status",
    "PUT:/apis/certificates.k8s.io/v1/certificatesigningrequests/{name}/status",
]


def _with_prefixes(op_ids: list[str]) -> list[str]:
    return [
        f"{method}:{prefix}{path}"
        for op_id in op_ids
        for method, path in [op_id.split(":", 1)]
        for prefix in _PREFIXES
    ]


@pytest.mark.parametrize("op_id", _with_prefixes(_KEPT_WRITES))
def test_status_and_scale_writes_keep_their_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False
    assert ingested_op_is_credential_call(op_id + "/") is False
    assert credential_grant_reason(op_id) is None


@pytest.mark.parametrize(
    "op_id",
    [
        "GET:/api/v1/namespaces/{namespace}/pods/{name}/log",
        "GET:/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
        "GET:/api/v1/namespaces/{name}",
        "HEAD:/api/v1/namespaces/{namespace}/pods/{name}/eviction",
        "OPTIONS:/apis/widgets.example.test/v1/widgets/{name}/restart",
        "POST:/api/v1/accounts",
        f"PUT:{_PREFIX}/api/v1/accounts/{{id}}",
        "POST:/api/v1/accounts/{id}/reset",
    ],
)
def test_reads_and_other_apis_keep_their_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False


@pytest.mark.parametrize(
    "op_id",
    [
        "POST:/api/v1/namespaces/{namespace}/pods/{name}/ephemeralcontainers",
        "PUT:/apis/certificates.k8s.io/v1/certificatesigningrequests/{name}/approval",
        "GET:/api/v1/namespaces/{namespace}/pods/{name}/exec",
        "GET:/api/v1/namespaces/{namespace}/serviceaccounts/{name}/token",
    ],
)
def test_the_paths_that_already_wait_still_wait(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _with_prefixes(_KEPT_WRITES[:2]))
async def test_a_service_login_with_a_grant_runs_a_status_or_scale_write(op_id: str) -> None:
    row = _ingested(op_id)
    row.safety_level, row.requires_approval = "caution", True
    async with get_sessionmaker()() as session, session.begin():
        session.add(row)
    params = {"namespace": "apps", "name": "a"}
    verdict, reason = await _verdict(_service(), row, params)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL, reason
    await _name_in_every_grant([row])
    verdict, reason = await _verdict(_service(), row, params)
    assert verdict is PermissionVerdict.AUTO_EXECUTE, reason


@pytest.mark.parametrize("op_id", [_EVICTION, _FINALIZE, _ADD_ON_ACTION])
def test_the_class_the_agent_sees_does_not_read_the_rule(
    monkeypatch: pytest.MonkeyPatch, op_id: str
) -> None:
    """The op class that names the call in the agent's preview, card and search is the same."""
    seen = (resolve_op_class(op_id, None), classify_op(op_id))
    monkeypatch.setattr(op_classes, "_CREDENTIAL_CAPABLE_MATCHES", [])
    assert op_classes.is_credential_capable(op_id) is False
    assert (resolve_op_class(op_id, None), classify_op(op_id)) == seen


#: Writes whose last segment ``status`` or ``scale`` comes right after a
#: ``namespaces/{namespace}`` part, so the path also reads as a collection.
_COLLECTION_LIKE_WRITES: list[str] = [
    "PUT:/api/v1/namespaces/{namespace}/scale",
    "POST:/apis/apps/v1/namespaces/{namespace}/status",
    "PATCH:/apis/apps/v1/namespaces/{namespace}/scale",
    "POST:/apis/widgets.example.test/v1/namespaces/{namespace}/status",
]


@pytest.mark.parametrize("op_id", _with_prefixes(_COLLECTION_LIKE_WRITES))
def test_a_status_or_scale_path_that_reads_as_a_collection_waits(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is True
    assert credential_grant_reason(op_id) is not None


@pytest.mark.parametrize("op_id", _with_prefixes(["PUT:/api/v1/namespaces/{namespace}/status"]))
def test_the_status_of_a_core_namespace_keeps_its_rules(op_id: str) -> None:
    assert ingested_op_is_credential_call(op_id) is False
    assert credential_grant_reason(op_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", _PREFIXES, ids=["plain", "prefix"])
async def test_a_status_path_that_reads_as_a_collection_waits_for_every_login(
    prefix: str,
) -> None:
    row = await _store(f"POST:{prefix}/apis/apps/v1/namespaces/{{namespace}}/status")
    params = {"namespace": "apps", "name": "a", "cluster": "c-1"}
    await _assert_waits_for_every_login(row, params)
