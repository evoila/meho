# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vRA 8 reconcile lane for the image ops (image profiles).

The four ``vcfa.vra8.*`` image ops run the handlers of
:mod:`meho_backplane.connectors.vcf_automation._images`, which they share
with VCF Automation 9. This lane sweeps the path constants that handler
module uses (live, never a copy), pins the ``METHOD:/path`` manifest, and
checks every call against the pinned vRA IaaS API file
(``vra-sdk-go`` v0.6.5 ``vra-iaas.json``, a Swagger 2.0 file of the
vRA 8 IaaS API) when the spec shelf is set up. Without the shelf the
second test skips; CI runs it. Parse-only: no DB, no network.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_automation import _images as images_module
from meho_backplane.connectors.vra8.image_ops import VRA8_IMAGE_OPS
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec
from tests.test_connectors_vcf_automation_spec_reconcile import _swagger2_served_op_ids

_SPEC_DIR = "vcf-automation-9.0"
_SPEC_FILE = "swagger-vra-sdk-go-v0.6.5/vra-iaas.json"

#: HTTP method(s) per path constant of the handler module. A new constant
#: missing here fails the sweep, so its method is mapped on purpose.
_METHODS_BY_CONSTANT: dict[str, frozenset[str]] = {
    "TENANT_IMAGE_PROFILES_PATH": frozenset({"GET"}),
    "TENANT_IMAGE_PROFILE_PATH": frozenset({"GET", "PATCH"}),
    "TENANT_FABRIC_IMAGES_PATH": frozenset({"GET"}),
    "TENANT_FABRIC_IMAGE_PATH": frozenset({"GET"}),
    "TENANT_IMAGE_ENUMERATION_PATH": frozenset({"POST"}),
    "TENANT_REQUEST_TRACKER_PATH": frozenset({"GET"}),
}


def _declared_op_ids() -> set[str]:
    """Every ``METHOD:/path`` the image handlers send."""
    declared: set[str] = set()
    for name, path in vars(images_module).items():
        if not (name.endswith("_PATH") and isinstance(path, str) and path.startswith("/")):
            continue
        methods = _METHODS_BY_CONSTANT.get(name)
        if methods is None:
            raise AssertionError(
                f"_images.{name} has no entry in _METHODS_BY_CONSTANT; map its HTTP method "
                "there so this lane keeps covering it."
            )
        declared.update(f"{method}:{path}" for method in methods)
    return declared


def test_vra8_image_op_id_manifest_is_pinned() -> None:
    assert _declared_op_ids() == {
        "GET:/iaas/api/image-profiles",
        "GET:/iaas/api/image-profiles/{id}",
        "PATCH:/iaas/api/image-profiles/{id}",
        "GET:/iaas/api/fabric-images",
        "GET:/iaas/api/fabric-images/{id}",
        "POST:/iaas/api/cloud-accounts/{id}/private-image-enumeration",
        "GET:/iaas/api/request-tracker/{id}",
    }
    assert {op.op_id for op in VRA8_IMAGE_OPS} == {
        "vcfa.vra8.image_profile.list",
        "vcfa.vra8.fabric_image.list",
        "vcfa.vra8.cloud_account.enumerate_images",
        "vcfa.vra8.image_profile.mapping.update",
    }


def test_vra8_image_calls_are_served_by_the_pinned_iaas_spec() -> None:
    """Every image call is in the pinned vRA IaaS API file (skips without the shelf)."""
    served = _swagger2_served_op_ids(require_shelf_spec(_SPEC_DIR, _SPEC_FILE))
    assert_op_ids_served(_declared_op_ids(), served, spec_label=f"{_SPEC_DIR}/{_SPEC_FILE}")
