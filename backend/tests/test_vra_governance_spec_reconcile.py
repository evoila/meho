# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reconcile lane for the shared approval, policy, ABX and subscription calls.

The nine ops are written once (``vmware_shared.vra_governance`` and
``vmware_shared.vra_extensibility``) and registered on the vRA 8 and the VCF
Automation connector. So one lane covers both connectors. It reads the live
``*_PATH`` constants of the two shared modules (never a copy) and:

* pins the ``METHOD:/path`` manifest, with no shelf needed;
* checks that every op of both connectors uses a swept constant;
* checks the policy calls against ``vra-catalog-deployment.json`` of the
  pinned ``vra-sdk-go`` v0.6.5 shelf;
* checks every call against the vendor's own reference lists on the shelf,
  one set per VCF Automation version (9.0, 9.1 and 9.1.1): the approval,
  policy and ABX lists, and on 9.1 and 9.1.1 the event broker list too (the
  vendor publishes no 9.0 event broker reference). Each list was made from
  the operation index of the vendor's public API reference pages.

The shelf-backed checks skip when the shelf is not set up
(``tests/_spec_shelf.py``); CI lists every file they read in its
required-file check, so there they run.

**Evidenced exclusion: three event broker calls.** No public vendor
reference lists ``GET /event-broker/api/subscriptions``,
``POST /event-broker/api/subscriptions`` or
``PATCH /event-broker/api/subscriptions/{id}``: the 9.1 and 9.1.1 Event
Broker Service API indexes list only the read and the delete of one
subscription and the topic reads, 9.0 has no event broker reference, and the
portal has no 8.x event broker reference (its "Event Broker Service API"
covers 7.x and uses other paths). The three calls are the ones the Task
names; they stay pinned in :data:`_NOT_IN_ANY_VENDOR_REFERENCE`, and the lane
fails the day a vendor list serves one of them, so the exclusion is removed
then. A live test after the merge proves them on Aria Automation 8.18 and
VCF Automation 9.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

import pytest

from meho_backplane.connectors.vcf_automation import VCFA_GOVERNANCE_OPS
from meho_backplane.connectors.vcf_automation._routing import plane_for_path
from meho_backplane.connectors.vmware_shared import vra_extensibility, vra_governance
from meho_backplane.connectors.vra8 import VRA8_GOVERNANCE_OPS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

if TYPE_CHECKING:
    from pathlib import Path

_SPEC_DIR: Final[str] = "vcf-automation-9.0"
_SPEC_FILE: Final[str] = "swagger-vra-sdk-go-v0.6.5/vra-catalog-deployment.json"

#: The Swagger 2.0 path-item keys that are HTTP operations.
_SWAGGER2_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch"}
)

#: HTTP method(s) per path constant of the two shared modules. A new
#: constant missing here fails the sweep, so its method is mapped on purpose.
_PATH_METHODS: Final[dict[str, frozenset[str]]] = {
    "APPROVALS_PATH": frozenset({"GET"}),
    "APPROVAL_PATH": frozenset({"GET"}),
    "APPROVAL_ACTION_PATH": frozenset({"POST"}),
    "POLICIES_PATH": frozenset({"GET", "POST"}),
    "POLICY_PATH": frozenset({"GET"}),
    "ABX_ACTIONS_PATH": frozenset({"GET"}),
    "ABX_ACTION_PATH": frozenset({"GET"}),
    "ABX_ACTION_RUNS_PATH": frozenset({"POST"}),
    "ABX_RUN_PATH": frozenset({"GET"}),
    "SUBSCRIPTIONS_PATH": frozenset({"GET", "POST"}),
    "SUBSCRIPTION_PATH": frozenset({"GET", "PATCH", "DELETE"}),
    "EVENT_TOPIC_PATH": frozenset({"GET"}),
}

#: The calls that are in the pinned ``vra-catalog-deployment.json``.
_SHELF_PREFIXES: Final[tuple[str, ...]] = ("/policy/api/",)

#: The vendor reference lists per VCF Automation version: shelf directory,
#: version, the path prefixes its lists cover, and the services whose list
#: it reads (``vcf-automation-<version>-vm-apps-org-<service>-reference-operations.tsv``).
_VENDOR_LISTS: Final[tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]] = (
    (
        "vcf-automation-9.0-reference",
        "9.0",
        ("/approval/api/", "/policy/api/", "/abx/api/"),
        ("approval", "policies", "abx"),
    ),
    (
        "vcf-automation-9.1",
        "9.1",
        ("/approval/api/", "/policy/api/", "/abx/api/", "/event-broker/api/"),
        ("approval", "policies", "abx", "event-broker-service-api"),
    ),
    (
        "vcf-automation-9.1.1",
        "9.1.1",
        ("/approval/api/", "/policy/api/", "/abx/api/", "/event-broker/api/"),
        ("approval", "policies", "abx", "event-broker-service-api"),
    ),
)

#: The calls no public vendor reference lists (module docstring). Pinned, so
#: adding a fourth is a conscious change, and the vendor-list check fails
#: when a list starts to serve one of them.
_NOT_IN_ANY_VENDOR_REFERENCE: Final[frozenset[str]] = frozenset(
    {
        "GET:/event-broker/api/subscriptions",
        "POST:/event-broker/api/subscriptions",
        "PATCH:/event-broker/api/subscriptions/{id}",
    }
)

_BRACES: Final[re.Pattern[str]] = re.compile(r"\{([^}/]+)\}")


def _path_constants() -> dict[str, str]:
    found: dict[str, str] = {}
    for module in (vra_governance, vra_extensibility):
        for name, value in vars(module).items():
            if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/"):
                found[name] = value
    return found


def _declared_op_ids() -> set[str]:
    declared: set[str] = set()
    for name, path in _path_constants().items():
        methods = _PATH_METHODS.get(name)
        if methods is None:
            raise AssertionError(f"{name} has no entry in _PATH_METHODS; map its HTTP method")
        declared.update(f"{method}:{path}" for method in methods)
    return declared


def test_path_constant_names_are_pinned() -> None:
    assert sorted(_path_constants()) == sorted(_PATH_METHODS)


def test_shared_op_id_manifest_is_pinned() -> None:
    assert _declared_op_ids() == {
        "GET:/approval/api/approvals",
        "GET:/approval/api/approvals/{id}",
        "POST:/approval/api/approvals/action",
        "GET:/policy/api/policies",
        "POST:/policy/api/policies",
        "GET:/policy/api/policies/{id}",
        "GET:/abx/api/resources/actions",
        "GET:/abx/api/resources/actions/{id}",
        "POST:/abx/api/resources/actions/{id}/action-runs",
        "GET:/abx/api/resources/action-runs/{id}",
        "GET:/event-broker/api/subscriptions",
        "POST:/event-broker/api/subscriptions",
        "GET:/event-broker/api/subscriptions/{id}",
        "PATCH:/event-broker/api/subscriptions/{id}",
        "DELETE:/event-broker/api/subscriptions/{id}",
        "GET:/event-broker/api/topics/{id}",
    }


def test_every_op_uses_a_swept_path() -> None:
    swept = set(_path_constants().values())
    unswept = {
        spec.op_id: spec.path
        for spec in (*VCFA_GOVERNANCE_OPS, *VRA8_GOVERNANCE_OPS)
        if spec.path not in swept
    }
    assert not unswept


def test_every_call_rides_the_vcfa_tenant_login() -> None:
    assert {plane_for_path(path) for path in _path_constants().values()} == {"tenant"}


def _swagger2_served_op_ids(spec_path: Path) -> set[str]:
    document = json.loads(spec_path.read_text(encoding="utf-8"))
    if document.get("swagger") != "2.0":
        pytest.fail(f"{spec_path.name}: expected a Swagger 2.0 document; the shelf file moved")
    base = str(document.get("basePath") or "").rstrip("/")
    served = {
        f"{method.upper()}:{base}{path}"
        for path, item in document.get("paths", {}).items()
        for method in item
        if method.lower() in _SWAGGER2_METHODS
    }
    if not served:
        pytest.fail(f"{spec_path.name}: read zero operations; the shelf file layout moved")
    return served


def _without_braces(op_id: str) -> str:
    """``GET:/x/{id}`` as ``GET:/x/id``: the vendor's index drops the braces."""
    return _BRACES.sub(r"\1", op_id)


def _vendor_list_op_ids(list_path: Path, label: str) -> set[str]:
    """The ``METHOD:/path`` set of one shelf reference list (TSV), braces dropped."""
    lines = list_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t")[:2] != ["method", "path"]:
        pytest.fail(f"{label}: expected a TSV header starting with 'method<TAB>path'.")
    served = {
        _without_braces(f"{cols[0].upper()}:{cols[1]}")
        for cols in (line.split("\t") for line in lines[1:])
        if len(cols) >= 2
    }
    if not served:
        pytest.fail(f"{label}: read zero operations; the list layout moved.")
    return served


def test_every_call_has_a_vendor_list_or_is_an_evidenced_exclusion() -> None:
    """No call is left out by accident: each prefix has a vendor list on 9.1.1."""
    declared = _declared_op_ids()
    assert declared >= _NOT_IN_ANY_VENDOR_REFERENCE
    covered = max(_VENDOR_LISTS, key=lambda row: len(row[2]))[2]
    uncovered = {op_id for op_id in declared if not op_id.partition(":")[2].startswith(covered)}
    assert not uncovered, f"no vendor list covers {sorted(uncovered)}"


@pytest.mark.parametrize(
    ("spec_dir", "version", "prefixes", "services"),
    _VENDOR_LISTS,
    ids=[version for _, version, _, _ in _VENDOR_LISTS],
)
def test_calls_are_in_the_vendor_reference_lists_of_each_version(
    spec_dir: str, version: str, prefixes: tuple[str, ...], services: tuple[str, ...]
) -> None:
    """Every call a version's vendor lists cover is in them, except the pinned three.

    Skips when the spec shelf is not set up (``tests/_spec_shelf.py``); CI runs it.
    """
    served: set[str] = set()
    for service in services:
        list_file = f"vcf-automation-{version}-vm-apps-org-{service}-reference-operations.tsv"
        served |= _vendor_list_op_ids(
            require_shelf_spec(spec_dir, list_file), f"{spec_dir}/{list_file}"
        )
    in_scope = {
        op_id for op_id in _declared_op_ids() if op_id.partition(":")[2].startswith(prefixes)
    }
    excluded = {_without_braces(op_id) for op_id in _NOT_IN_ANY_VENDOR_REFERENCE}
    now_listed = excluded & served
    assert not now_listed, (
        f"the {version} vendor lists now serve {sorted(now_listed)}; remove them from "
        "_NOT_IN_ANY_VENDOR_REFERENCE"
    )
    declared = {_without_braces(op_id) for op_id in in_scope} - excluded
    assert_op_ids_served(declared, served, spec_label=f"{spec_dir} vendor reference lists")


def test_policy_calls_are_served_by_the_pinned_catalog_spec() -> None:
    """Skips when the spec shelf is not set up (``tests/_spec_shelf.py``); CI runs it."""
    spec = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    declared = {
        op_id for op_id in _declared_op_ids() if op_id.partition(":")[2].startswith(_SHELF_PREFIXES)
    }
    assert declared
    assert_op_ids_served(
        declared, _swagger2_served_op_ids(spec), spec_label=f"{_SPEC_DIR}/{_SPEC_FILE}"
    )
