# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``k8s.apply`` refuses a manifest it cannot apply before the call waits.

What these tests prove, through the real dispatch over a stand-in cluster
(made-up names):

* each form that the run cannot parse is refused at once, with the same
  text on the call and on ``preview_operation``'s path; no approval
  request is stored, no audit row of a waiting call is written, and the
  cluster gets no request;
* three documents split by ``---`` wait for approval once, and after the
  approval they are applied in order;
* an empty text and a list as the value of ``manifest`` are still refused
  by the params check;
* a manifest that passes the check passes the parse step of the run;
* a manifest that is not valid YAML or JSON is refused with the document
  number, the line, the column and one fixed phrase, and no value of it is
  in an answer, a preview answer, an audit row or a log.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import yaml
from sqlalchemy import func, select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.kubernetes._apply_manifest import (
    NOT_SHOWN,
    ApplyManifestError,
    _parse_manifest,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings
from tests.test_connectors_k8s_finalizer_remove import _StubTarget

_OP = "k8s.apply"
_CONNECTOR_ID = "k8s-1.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000006982")
_TARGET = _StubTarget(tenant_id=_TENANT_ID)

_FORM = (
    "Send one text with the documents split by `---`. "
    "Each document has `apiVersion`, `kind` and `metadata.name`"
)

_NAMESPACE = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: web\n"
_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: web\n"
    "spec:\n  replicas: 1\n"
)
_SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: web\n"
_THREE = "---\n".join([_NAMESPACE, _DEPLOYMENT, _SERVICE])

#: (manifest, the part of the reason that names what is wrong)
_REFUSED: dict[str, tuple[str, str]] = {
    "json-array": ('[{"apiVersion": "v1", "kind": "Namespace"}]', "document 1 is not a mapping"),
    "yaml-list": ("- apiVersion: v1\n  kind: Namespace\n", "document 1 is not a mapping"),
    "one-word": ("hello", "document 1 is not a mapping"),
    "list-without-name": (
        "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: v1\n    kind: Namespace\n"
        "    metadata: {name: web}\n",
        "document 1 (apiVersion v1, kind List) is a list object",
    ),
    "no-kind": (
        "apiVersion: v1\nmetadata:\n  name: web\n",
        "document 1 (apiVersion v1) is missing kind",
    ),
    "no-api-version": (
        "kind: Namespace\nmetadata:\n  name: web\n",
        "document 1 (kind Namespace) is missing apiVersion",
    ),
    "no-name": (
        "apiVersion: v1\nkind: Namespace\nmetadata: {}\n",
        "document 1 (apiVersion v1, kind Namespace) is missing metadata.name",
    ),
    "only-spaces": ("   \n  \n", "the text has no documents"),
    "only-comments": ("# a\n# b\n", "the text has no documents"),
    "only-separators": ("---\n---\n---\n", "the text has no documents"),
    "bad-yaml-in-the-middle": (
        _NAMESPACE + "---\na: [b\n---\n" + _SERVICE,
        "document 2 is not valid YAML or JSON (a structure YAML refuses, line 7, column 1)",
    ),
    "tab-before-name-in-document-2": (
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: web}\n---\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n\tname: web\n",
        "document 2 is not valid YAML or JSON (a token YAML refuses, line 8, column 1)",
    ),
    "no-kind-in-the-middle": (
        _NAMESPACE + "---\nmetadata: {name: x}\n---\n" + _SERVICE,
        "document 2 is missing apiVersion",
    ),
    "word-in-the-middle": (
        _NAMESPACE + "---\nhello\n---\n" + _SERVICE,
        "document 2 is not a mapping",
    ),
    "metadata-is-text": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: web\n",
        "document 1 (apiVersion apps/v1, kind Deployment) has a metadata that is not a mapping",
    ),
    "kind-is-a-list": (
        "apiVersion: v1\nkind: [Namespace]\nmetadata: {name: web}\n",
        "document 1 (apiVersion v1, kind (not shown)) has a kind that is not a text",
    ),
    "api-version-is-a-number": (
        "apiVersion: 1\nkind: Namespace\nmetadata: {name: web}\n",
        "document 1 (apiVersion (not shown), kind Namespace) has an apiVersion that is not a text",
    ),
    "name-is-a-number": (
        "apiVersion: v1\nkind: Namespace\nmetadata: {name: 123}\n",
        "document 1 (apiVersion v1, kind Namespace) has a metadata.name that is not a text"
        " (put it in quotes)",
    ),
    "name-is-a-date-without-quotes": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: 2026-11-26}\n",
        "document 1 (apiVersion v1, kind ConfigMap) has a metadata.name that is not a text"
        " (put it in quotes)",
    ),
    "name-is-a-year-without-quotes": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: 2024}\n",
        "document 1 (apiVersion v1, kind ConfigMap) has a metadata.name that is not a text"
        " (put it in quotes)",
    ),
    "date-that-does-not-exist-in-document-2": (
        _NAMESPACE + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\n"
        "data:\n  day: 2026-02-30\n",
        "document 2 is not valid YAML or JSON (a value YAML cannot build)",
    ),
    "control-character-in-document-2": (
        _NAMESPACE + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: w" + chr(11) + "b}\n",
        "the text has a character that YAML does not allow, for example a control character",
    ),
    "empty-kind": (
        "apiVersion: v1\nkind: ''\nmetadata: {name: web}\n",
        "document 1 (apiVersion v1) is missing kind",
    ),
    "kind-list-with-name": (
        "apiVersion: v1\nkind: List\nmetadata: {name: web}\nitems: []\n",
        "document 1 (apiVersion v1, kind List) is a list object",
    ),
    "namespace-list-with-items": (
        "apiVersion: v1\nkind: NamespaceList\nmetadata: {name: web}\nitems: []\n",
        "document 1 (apiVersion v1, kind NamespaceList) is a list object",
    ),
    "deployment-list-with-items": (
        "apiVersion: apps/v1\nkind: DeploymentList\nmetadata: {name: web}\nitems: []\n",
        "document 1 (apiVersion apps/v1, kind DeploymentList) is a list object",
    ),
    "custom-resource-ending-in-list-with-items": (
        "apiVersion: example.com/v1\nkind: ShoppingList\nmetadata: {name: web}\nitems: [milk]\n",
        "document 1 (apiVersion example.com/v1, kind ShoppingList) is a list object",
    ),
    "custom-kind-named-list-with-items": (
        "apiVersion: example.test/v1\nkind: List\nmetadata: {name: web}\nitems: [milk]\n",
        "document 1 (apiVersion example.test/v1, kind List) is a list object",
    ),
    "custom-resource-with-items": (
        "apiVersion: example.com/v1\nkind: Cart\nmetadata: {name: web}\nitems: [milk]\n",
        "document 1 (apiVersion example.com/v1, kind Cart) is a list object",
    ),
    "custom-resource-in-a-k8s-io-group-with-items": (
        "apiVersion: shop.example.k8s.io/v1\nkind: ShoppingList\nmetadata: {name: web}\n"
        "items: [milk]\n",
        "document 1 (apiVersion shop.example.k8s.io/v1, kind ShoppingList) is a list object",
    ),
    "config-map-with-items": (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\ndata: {a: b}\nitems: [milk]\n",
        "document 1 (apiVersion v1, kind ConfigMap) is a list object",
    ),
}


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_kubernetes_registry() -> Iterator[None]:
    clear_registry()
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def cluster(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Register the ops; any use of the cluster client is a call to this stand-in."""
    from meho_backplane.operations import typed_register as tr_module
    from meho_backplane.operations._handler_resolve import reset_handler_cache

    reset_handler_cache()
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await KubernetesConnector.register_operations()
    get_client = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(KubernetesConnector, "_get_api_client", get_client)
    return get_client


def _operator() -> Operator:
    return Operator(
        sub="op-apply-form",
        name="Apply Form Test Operator",
        email=None,
        raw_jwt="op.apply.form.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _call(params: dict[str, Any]) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )


async def _count(model: Any) -> int:
    async with get_sessionmaker()() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.parametrize("name", list(_REFUSED))
async def test_a_manifest_the_run_cannot_parse_is_refused_before_the_wait(
    name: str, cluster: AsyncMock
) -> None:
    manifest, wrong = _REFUSED[name]
    params = {"manifest": manifest}

    result = await _call(params)
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )

    assert result.status == "denied", result
    assert result.extras["error_code"] == "refused_before_approval"
    reason = result.extras["reason"]
    assert reason == f"The manifest cannot be applied: {wrong}. {_FORM}"
    assert preview["error"] == result.error
    assert preview["extras"]["reason"] == reason
    assert await _count(ApprovalRequest) == 0
    async with get_sessionmaker()() as s:
        audit = list((await s.execute(select(AuditLog))).scalars().all())
    assert [r.payload["result_status"] for r in audit] == ["denied"]
    cluster.assert_not_awaited()


async def _refusal_seen_everywhere(manifest: str) -> tuple[str, str, str, str]:
    """The reason, the preview reason, the audit rows and the logs of one refused call."""
    params = {"manifest": manifest}
    with capture_logs() as logs:
        result = await _call(params)
        preview = await preview_dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP,
            target=_TARGET,
            params=params,
        )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "refused_before_approval"
    assert await _count(ApprovalRequest) == 0
    async with get_sessionmaker()() as s:
        audit = list((await s.execute(select(AuditLog))).scalars().all())
    assert [r.payload["result_status"] for r in audit] == ["denied"]
    audit_text = json.dumps([r.payload for r in audit], default=str)
    return result.extras["reason"], preview["extras"]["reason"], audit_text, repr(logs)


#: A group of lower-case letters and dots only, one character over the limit of 253.
_GROUP_254 = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 62])


def _no_metadata_name(api_version: str, kind: str) -> str:
    return f"apiVersion: {api_version}\nkind: {kind}\nmetadata: {{}}\n"


@pytest.mark.parametrize(
    ("manifest", "marker", "shown"),
    [
        (
            "apiVersion: v1\nkind: {x: marker-kind-mapping}\nmetadata: {}\n",
            "marker-kind-mapping",
            "(apiVersion v1, kind (not shown)) has a kind that is not a text",
        ),
        (
            "apiVersion: {x: marker-api-mapping}\nkind: Namespace\nmetadata: {}\n",
            "marker-api-mapping",
            "(apiVersion (not shown), kind Namespace) has an apiVersion that is not a text",
        ),
        (
            _no_metadata_name("v1", "K" + "m" * 63),
            "K" + "m" * 63,
            "(apiVersion v1, kind (not shown)) is missing metadata.name",
        ),
        (
            _no_metadata_name("v1", "'marker kind'"),
            "marker kind",
            "(apiVersion v1, kind (not shown)) is missing metadata.name",
        ),
        (
            _no_metadata_name("'marker api/v1'", "Namespace"),
            "marker api",
            "(apiVersion (not shown), kind Namespace) is missing metadata.name",
        ),
        (
            _no_metadata_name("Marker.Group/v1", "Namespace"),
            "Marker.Group",
            "(apiVersion (not shown), kind Namespace) is missing metadata.name",
        ),
        (
            _no_metadata_name("apps/Marker-Version", "Namespace"),
            "Marker-Version",
            "(apiVersion (not shown), kind Namespace) is missing metadata.name",
        ),
        (
            _no_metadata_name(_GROUP_254 + "/v1", "Namespace"),
            _GROUP_254,
            "(apiVersion (not shown), kind Namespace) is missing metadata.name",
        ),
        (
            _no_metadata_name("v1", "Namespace") + "spec: {x: marker-other-value}\n",
            "marker-other-value",
            "(apiVersion v1, kind Namespace) is missing metadata.name",
        ),
        (
            "apiVersion: v1\nkind: Namespace\nmetadata: marker-metadata-text\n",
            "marker-metadata-text",
            "(apiVersion v1, kind Namespace) has a metadata that is not a mapping",
        ),
    ],
    ids=[
        "kind-mapping",
        "api-version-mapping",
        "kind-64-chars",
        "kind-with-space",
        "api-version-with-space",
        "group-with-capitals",
        "version-with-capitals",
        "group-254-chars",
        "other-value",
        "metadata-text",
    ],
)
async def test_the_refusal_shows_no_unsafe_value_of_the_manifest(
    manifest: str, marker: str, shown: str, cluster: AsyncMock
) -> None:
    reason, preview_reason, audit, logs = await _refusal_seen_everywhere(manifest)
    assert reason == f"The manifest cannot be applied: document 1 {shown}. {_FORM}"
    assert preview_reason == reason
    for text in (reason, audit, logs):
        assert marker not in text
    if "(not shown)" in reason:
        assert "(not shown)" in audit
    cluster.assert_not_awaited()


async def test_a_yaml_error_text_is_not_shown(cluster: AsyncMock) -> None:
    reason, preview_reason, audit, logs = await _refusal_seen_everywhere(
        _NAMESPACE + "---\na: [marker-yaml\n"
    )
    assert reason == (
        "The manifest cannot be applied: document 2 is not valid YAML or JSON "
        f"(a structure YAML refuses, line 7, column 1). {_FORM}"
    )
    assert preview_reason == reason
    for text in (reason, audit, logs):
        assert "marker-yaml" not in text


@pytest.mark.parametrize(
    ("api_version", "kind"),
    [
        ("apps/v1", "Deployment"),
        ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"),
        ("v1", "Namespace"),
    ],
)
async def test_a_kubernetes_api_version_and_kind_show_whole(
    api_version: str, kind: str, cluster: AsyncMock
) -> None:
    reason, preview_reason, audit, _logs = await _refusal_seen_everywhere(
        _no_metadata_name(api_version, kind)
    )
    assert reason == (
        f"The manifest cannot be applied: document 1 (apiVersion {api_version}, kind {kind}) "
        f"is missing metadata.name. {_FORM}"
    )
    assert preview_reason == reason
    assert api_version in audit
    assert kind in audit


async def test_a_long_valid_group_and_kind_keep_the_whole_form(cluster: AsyncMock) -> None:
    group = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    assert len(group) == 253
    kind = "K" + "k" * 62
    reason, preview_reason, _audit, _logs = await _refusal_seen_everywhere(
        _no_metadata_name(f"{group}/v1", kind)
    )
    assert len(reason) <= 256
    assert reason.endswith(f"is missing metadata.name. {_FORM}")
    assert reason.startswith("The manifest cannot be applied: document 1 (apiVersion (not shown)")
    assert preview_reason == reason


def test_a_group_over_253_characters_is_not_safe() -> None:
    # The 256-character cap of the text hides such a value too; this checks the shape rule itself.
    with pytest.raises(ApplyManifestError) as raised:
        _parse_manifest(_no_metadata_name(_GROUP_254 + "/v1", "Namespace"))
    assert raised.value.api_version == NOT_SHOWN


def test_the_run_names_the_same_document_as_the_check() -> None:
    with pytest.raises(ApplyManifestError) as raised:
        _parse_manifest(_NAMESPACE + "---\nhello\n")
    assert str(raised.value) == "The manifest cannot be applied: document 2 is not a mapping"


async def test_a_date_that_does_not_exist_is_not_shown(cluster: AsyncMock) -> None:
    reason, preview_reason, audit, logs = await _refusal_seen_everywhere(
        _NAMESPACE + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\n"
        "data:\n  day: 2026-02-30\n"
    )
    assert reason == (
        "The manifest cannot be applied: document 2 is not valid YAML or JSON "
        f"(a value YAML cannot build). {_FORM}"
    )
    assert preview_reason == reason
    for text in (reason, audit, logs):
        assert "2026-02-30" not in text


_MADE_UP = "qv7d3e9a0b41c2f58e6a1x"

#: (the faulty line of document 2, with the made-up value; the fixed phrase and place)
_YAML_KINDS: dict[str, tuple[str, str]] = {
    "bad-token": (f"  note: {_MADE_UP}: x\n", "a token YAML refuses, line 10, column 31"),
    "flow-mapping-not-closed": (
        f"  note: {{a: {_MADE_UP}\n",
        "a structure YAML refuses, line 11, column 1",
    ),
    "undefined-alias": (
        f"  note: *{_MADE_UP}\n",
        "an alias or anchor YAML refuses, line 10, column 9",
    ),
    "unknown-tag": (
        f"  note: !{_MADE_UP} x\n",
        "a tag or value YAML cannot build, line 10, column 9",
    ),
    "bool-tag-with-a-text": (f"  note: !!bool {_MADE_UP}\n", "a value YAML cannot build"),
}


@pytest.mark.parametrize("name", list(_YAML_KINDS))
async def test_each_kind_of_yaml_error_has_its_phrase_and_shows_no_value(
    name: str, cluster: AsyncMock
) -> None:
    line, why = _YAML_KINDS[name]
    manifest = (
        _NAMESPACE + "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\ndata:\n" + line
    )
    reason, preview_reason, audit, logs = await _refusal_seen_everywhere(manifest)
    assert reason == (
        f"The manifest cannot be applied: document 2 is not valid YAML or JSON ({why}). {_FORM}"
    )
    assert preview_reason == reason
    for text in (reason, audit, logs):
        assert _MADE_UP not in text
    cluster.assert_not_awaited()


def test_the_largest_numbers_keep_the_refusal_within_256_characters() -> None:
    from meho_backplane.connectors.kubernetes.ops_write_preview import _APPLY_FORM

    error = ApplyManifestError(
        "is not valid YAML or JSON",
        number=999999,
        reason="an alias or anchor YAML refuses",
        position="line 99999999, column 999999",
    )
    tail = f". {_APPLY_FORM}"
    text = error.text(tail=tail)
    assert len(text) <= 256
    assert text.endswith(tail)
    # A longer tail leaves out the line and the column first, and stays whole.
    longer = tail + " " + "x" * 20
    shorter = error.text(tail=longer)
    assert len(shorter) <= 256
    assert shorter.endswith(longer)
    assert "line" not in shorter.removesuffix(longer)
    assert "(an alias or anchor YAML refuses)" in shorter


@pytest.mark.parametrize("name", ["'2026-11-26'", '"2024"'], ids=["date", "year"])
def test_a_name_in_quotes_passes_the_check(name: str) -> None:
    docs = _parse_manifest(f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {name}}}\n")
    assert docs[0]["metadata"]["name"] == name.strip("'\"")


async def test_a_kind_ending_in_list_without_items_waits_once(cluster: AsyncMock) -> None:
    manifest = "apiVersion: example.com/v1\nkind: ShoppingList\nmetadata: {name: web}\n"
    result = await _call({"manifest": manifest})
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1


async def test_a_custom_resource_with_items_under_spec_waits_once(cluster: AsyncMock) -> None:
    params = {
        "manifest": (
            "apiVersion: example.com/v1\nkind: ShoppingList\nmetadata: {name: web}\n"
            "spec:\n  items: [milk]\n"
        )
    }
    result = await _call(params)
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1
    assert preview["status"] == "ok", preview


async def test_a_custom_kind_named_list_without_items_waits_once(cluster: AsyncMock) -> None:
    """Recorded from a real API server, Kubernetes 1.37.3, 27 November 2026:
    it stores an object of a custom kind named `List`.
    """
    params = {
        "manifest": (
            "apiVersion: example.test/v1\nkind: List\nmetadata: {name: one, namespace: prod}\n"
            "spec: {size: 2}\n"
        )
    }
    result = await _call(params)
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1
    assert preview["status"] == "ok", preview


_COUNTER = "apiVersion: example.com/v1\nkind: Counter\nmetadata: {name: one}\nitems: 3\n"
_CONFIG_MAP = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: web}\ndata: {a: b}\n"


async def test_a_custom_resource_whose_items_is_a_number_waits_once(cluster: AsyncMock) -> None:
    params = {"manifest": _COUNTER}
    result = await _call(params)
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params=params,
    )
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1
    assert preview["status"] == "ok", preview


async def test_three_documents_with_items_a_number_in_the_middle_wait_once(
    cluster: AsyncMock,
) -> None:
    result = await _call({"manifest": "---\n".join([_CONFIG_MAP, _COUNTER, _CONFIG_MAP])})
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1


def _recorded(api_version: str, kind: str, rest: str) -> str:
    head = f"apiVersion: {api_version}\nkind: {kind}\n"
    return f"{head}metadata: {{name: one, namespace: prod}}\n{rest}"


#: (manifest, the API server refused it) for the 27 recorded cases, in their order.
_RECORDED_ANSWERS: list[tuple[str, bool]] = [
    (_recorded("example.com/v1", "ShoppingList", "items: [milk]\n"), True),
    (_recorded("example.com/v1", "Cart", "items: [milk]\n"), True),
    (_recorded("example.com/v1", "Bag", "items: [milk]\n"), True),
    (_recorded("example.com/v1", "Bag", "items:\n  - name: milk\n"), True),
    (_recorded("example.com/v1", "Crate", "items:\n  - name: milk\n"), True),
    (_recorded("example.com/v1", "Box", "items: [milk]\n"), True),
    (_recorded("example.com/v1", "ShoppingList", "items: []\n"), True),
    (_recorded("example.com/v1", "ShoppingList", "items: [milk]\n"), True),
    (_recorded("shop.example.k8s.io/v1", "ShoppingList", "items: [milk]\n"), True),
    (_recorded("v1", "ConfigMap", "data: {a: b}\nitems: [milk]\n"), True),
    (_recorded("apps/v1", "Deployment", "items: [milk]\n"), True),
    (_recorded("v1", "NamespaceList", "items: []\n"), True),
    (_recorded("example.com/v1", "ShoppingList", "spec:\n  items: [milk]\n"), False),
    (_recorded("v1", "ConfigMap", "data: {a: b}\n"), False),
    (_recorded("example.com/v1", "Basket", "items: [milk]\n"), True),
    (_recorded("example.com/v1", "Basket", "items: []\n"), True),
    (_recorded("example.com/v1", "Counter", "items: 3\n"), False),
    (_recorded("example.com/v1", "Label", "items: milk\n"), False),
    (_recorded("example.com/v1", "Folder", "items: {a: b}\n"), False),
    (_recorded("example.com/v1", "Basket", ""), False),
    (_recorded("example.com/v1", "Bag", "items: [milk]\n"), True),
    (_recorded("example.com/v1", "Bag", "items: []\n"), True),
    (_recorded("example.com/v1", "Bag", "items: 3\n"), False),
    (_recorded("example.com/v1", "Bag", "items: milk\n"), False),
    (_recorded("example.com/v1", "Bag", "items: {a: b}\n"), False),
    (_recorded("example.com/v1", "Bag", "items: null\n"), False),
    (_recorded("example.com/v1", "Bag", ""), False),
]


@pytest.mark.parametrize(
    ("manifest", "refused"), _RECORDED_ANSWERS, ids=[str(n) for n in range(1, 28)]
)
def test_the_list_object_check_matches_a_real_api_server(manifest: str, refused: bool) -> None:
    """Expected answers recorded from a real API server: Kubernetes 1.37.0 (a kind cluster),
    27 November 2026, server-side apply with and without a dry run.
    """
    if not refused:
        assert _parse_manifest(manifest)
        return
    with pytest.raises(ApplyManifestError) as raised:
        _parse_manifest(manifest)
    doc = yaml.safe_load(manifest)
    assert str(raised.value) == (
        "The manifest cannot be applied: document 1 "
        f"(apiVersion {doc['apiVersion']}, kind {doc['kind']}) is a list object"
    )


async def test_three_documents_wait_once_and_apply_in_order(
    cluster: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _call({"manifest": _THREE})
    assert result.status == "awaiting_approval", result
    assert await _count(ApprovalRequest) == 1

    seen: list[str] = []

    async def _apply_one(_dyn: Any, doc: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        seen.append(doc["kind"])
        return {"kind": doc["kind"]}

    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous._apply_one", _apply_one
    )
    monkeypatch.setattr(
        "meho_backplane.connectors.kubernetes.ops_write_dangerous.DynamicClient",
        AsyncMock(return_value=MagicMock()),
    )
    ran = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP,
        target=_TARGET,
        params={"manifest": _THREE},
        _approved=True,
    )
    assert ran.status == "ok", ran
    assert seen == ["Namespace", "Deployment", "Service"]


@pytest.mark.parametrize("manifest", ["", ["a"]], ids=["empty", "list-value"])
async def test_an_empty_text_and_a_list_are_still_refused_by_the_params_check(
    manifest: Any, cluster: AsyncMock
) -> None:
    result = await _call({"manifest": manifest})
    assert result.status != "awaiting_approval"
    assert result.extras.get("error_code") != "refused_before_approval"
    assert await _count(ApprovalRequest) == 0


@pytest.mark.parametrize(
    "manifest",
    [
        _NAMESPACE,
        _THREE,
        _NAMESPACE + "---\n" + "# note\n",
        '{"apiVersion": "v1", "kind": "X", "metadata": {"name": "a"}}',
    ],
    ids=["one", "three", "trailing-empty", "json"],
)
async def test_a_manifest_that_passes_the_check_passes_the_parse_step(
    manifest: str, cluster: AsyncMock
) -> None:
    from meho_backplane.connectors.kubernetes.ops_write_preview import _apply_park_check
    from meho_backplane.operations._preview import PreviewContext

    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP),
        connector_instance=None,
        operator=_operator(),
        target=_TARGET,
        params={"manifest": manifest},
        connector_id=_CONNECTOR_ID,
    )
    assert await _apply_park_check(ctx) is None
    assert _parse_manifest(manifest)


def test_the_manifest_hint_is_the_released_text() -> None:
    from meho_backplane.connectors.kubernetes.ops_write_meta import WRITE_DANGEROUS_OPS

    op = next(o for o in WRITE_DANGEROUS_OPS if o.op_id == _OP)
    assert op.llm_instructions is not None
    assert (
        op.llm_instructions["parameter_hints"]["manifest"]
        == "YAML/JSON; multi-doc supported via '---'."
    )
