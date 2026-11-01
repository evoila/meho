# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the vCenter install operations against the pinned vendor API files.

* An always-on test pins the vendor-call lists of the three changes, so a
  change to them is seen.
* The shelf tests read the pinned ``vcenter.yaml`` of vCenter 8.0 (U3) and
  9.0: every REST call is a path there, the specs MEHO sends have the fields
  of the vendor's request bodies, and the appliance states MEHO knows are the
  vendor's states. The file shows these fields at the top of the body. The
  setup service of a new appliance wants them under ``spec`` (see
  ``vcenter_deployment.setup_body``), so MEHO wraps them, and the tests check
  the fields inside ``spec``. The VI-JSON calls of the deploy are
  checked against the pinned 9.0 ``vi-json.yaml``. They skip when the spec
  shelf is not configured and run in CI.

vCenter 8.0 U1 to U3 VI-JSON calls are checked by the source scan of
``test_connectors_vmware_rest_8_0_spec_reconcile.py``, which finds the
deploy's VI-JSON methods in the connector source on its own.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.connectors.vmware_rest import vcenter_deployment
from meho_backplane.connectors.vmware_rest.composites import (
    _vcenter_answer,
    _vcenter_deploy,
    _vcenter_install,
)
from meho_backplane.connectors.vmware_rest.composites._vcenter_install_ops import (
    VCENTER_INSTALL_GOVERNED_SUBOPS,
)
from meho_backplane.operations.ingest import parse_openapi
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_VCENTER_SPECS = ("vcenter-8.0", "vcenter-9.0")

_REST_CALLS = (
    *_vcenter_install._SUB_OPS_VCENTER_INSTALL,
    *_vcenter_answer._SUB_OPS_VCENTER_DEPLOYMENT_ANSWER,
    *_vcenter_deploy._SUB_OPS_VCENTER_APPLIANCE_DEPLOY,
)


_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


def _shape(op_id: str) -> str:
    """The op id with each path placeholder name left out.

    vCenter 8.0 names a placeholder ``{download_session_id}``, 9.0
    ``{downloadSessionId}``: the path is the same.
    """
    return _PLACEHOLDER.sub("{}", op_id)


@functools.cache
def _rows(spec_path: Path) -> dict[str, Any]:
    rows = parse_openapi(
        f"file://{spec_path}",
        spec_source="spec:vcenter.yaml",
        content=spec_path.read_text(encoding="utf-8"),
    )
    return {_shape(row.op_id): row for row in rows}


def _body_fields(row: Any) -> set[str]:
    """The top-level fields of a row's request body; follows one ``$ref``."""
    schema = row.parameter_schema if isinstance(row.parameter_schema, dict) else {}
    body = schema.get("properties", {}).get("body", {})
    ref = body.get("$ref")
    if isinstance(ref, str):
        body = schema.get("components", {}).get("schemas", {}).get(ref.rsplit("/", 1)[-1], {})
    return set(body.get("properties") or {})


def test_the_vendor_call_lists_are_the_expected_sets() -> None:
    assert _vcenter_install._SUB_OPS_VCENTER_INSTALL == (
        "GET:/vcenter/deployment",
        "POST:/vcenter/deployment/install?action=check",
        "POST:/vcenter/deployment/install?action=start",
    )
    assert _vcenter_answer._SUB_OPS_VCENTER_DEPLOYMENT_ANSWER == (
        "GET:/vcenter/deployment",
        "GET:/vcenter/deployment/question",
        "POST:/vcenter/deployment/question?action=answer",
    )
    assert "POST:/content/library/item/download-session" in (
        _vcenter_deploy._SUB_OPS_VCENTER_APPLIANCE_DEPLOY
    )
    assert "POST:/ResourcePool/{moId}/ImportVApp" in (
        _vcenter_deploy._VIM_SUB_OPS_VCENTER_APPLIANCE_DEPLOY
    )
    assert set(VCENTER_INSTALL_GOVERNED_SUBOPS) == {
        "vmware.composite.vcenter.appliance.deploy",
        "vmware.composite.vcenter.install",
        "vmware.composite.vcenter.deployment.answer",
    }


@pytest.mark.parametrize("product_dir", _VCENTER_SPECS)
def test_every_rest_call_is_a_path_of_the_pinned_vcenter_file(product_dir: str) -> None:
    spec_path = require_shelf_spec(product_dir, "vcenter.yaml")
    assert_op_ids_served(
        {_shape(op_id) for op_id in _REST_CALLS},
        set(_rows(spec_path)),
        spec_label=f"{product_dir}/vcenter.yaml",
    )


def test_the_setup_posts_send_the_spec_under_one_spec_key() -> None:
    spec = _vcenter_install._install_spec({"standalone": {}}, "<password>")
    body = vcenter_deployment.setup_body(spec)
    assert body == {"spec": spec}
    assert set(spec) == {"auto_answer", "vcsa_embedded"}


@pytest.mark.parametrize("product_dir", _VCENTER_SPECS)
def test_the_specs_meho_sends_have_the_fields_of_the_vendor_bodies(product_dir: str) -> None:
    rows = _rows(require_shelf_spec(product_dir, "vcenter.yaml"))
    spec = _vcenter_install._install_spec({"standalone": {}}, "<password>")
    for op_id in (vcenter_deployment.OP_INSTALL_CHECK, vcenter_deployment.OP_INSTALL_START):
        fields = _body_fields(rows[op_id])
        assert set(spec) <= fields, op_id
        assert "spec" not in fields
    assert _body_fields(rows[vcenter_deployment.OP_QUESTION_ANSWER]) == {
        "question_id",
        "answer_val",
    }


@pytest.mark.parametrize("product_dir", _VCENTER_SPECS)
def test_meho_knows_every_vendor_appliance_state(product_dir: str) -> None:
    text = require_shelf_spec(product_dir, "vcenter.yaml").read_text(encoding="utf-8")
    for state in vcenter_deployment.VENDOR_STATES:
        assert f"- {state}" in text, f"{state} is not a state in {product_dir}/vcenter.yaml"
    for field in ("sso_admin_password", "partner_hostname", "sso_domain_name", "ceip_enabled"):
        assert f"{field}:" in text, f"{field} is not a field in {product_dir}/vcenter.yaml"


def test_the_vi_json_calls_of_the_deploy_are_post_paths_of_the_pinned_9_0_file() -> None:
    lines = require_shelf_spec("vcenter-9.0", "vi-json.yaml").read_text(encoding="utf-8")
    missing = [
        op_id
        for op_id in _vcenter_deploy._VIM_SUB_OPS_VCENTER_APPLIANCE_DEPLOY
        if f"\n  {op_id.partition(':')[2]}:\n    post:" not in lines
    ]
    assert not missing, f"the pinned 9.0 vi-json.yaml has no POST path for {missing}"
