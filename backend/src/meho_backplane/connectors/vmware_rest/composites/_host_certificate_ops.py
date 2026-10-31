# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows of the three host certificate changes.

``_register.py`` splats :data:`HOST_CERTIFICATE_COMPOSITES` into its
``_COMPOSITES`` table, and ``_governed_subops.py`` reads
:data:`HOST_CERTIFICATE_GOVERNED_SUBOPS`. Every change has
``requires_approval=True``. The rows share the group
:data:`~meho_backplane.connectors.vmware_rest.host_certificate.HOST_CERTIFICATES_GROUP_KEY`
with the two typed reads (certificate and TPM).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites import (
    _host_certificate,
    _host_certificate_install,
)
from meho_backplane.connectors.vmware_rest.composites._host_certificate_schemas import (
    CERTIFICATE_CSR_LLM_INSTRUCTIONS,
    CERTIFICATE_CSR_PARAMETER_SCHEMA,
    CERTIFICATE_CSR_RESPONSE_SCHEMA,
    CERTIFICATE_INSTALL_LLM_INSTRUCTIONS,
    CERTIFICATE_INSTALL_PARAMETER_SCHEMA,
    CERTIFICATE_INSTALL_RESPONSE_SCHEMA,
    CERTIFICATE_RENEW_LLM_INSTRUCTIONS,
    CERTIFICATE_RENEW_PARAMETER_SCHEMA,
    CERTIFICATE_RENEW_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import _OP_RETRIEVE_PROPERTIES
from meho_backplane.connectors.vmware_rest.host_certificate import HOST_CERTIFICATES_GROUP_KEY

__all__ = ["HOST_CERTIFICATE_COMPOSITES", "HOST_CERTIFICATE_GOVERNED_SUBOPS"]

_TAGS: Final = ("composite", "write", "host", "certificate", "vi-json")

HOST_CERTIFICATE_COMPOSITES: Final[tuple[dict[str, Any], ...]] = (
    {
        "op_id": _host_certificate.OP_ID_HOST_CERTIFICATE_RENEW,
        "handler": _host_certificate.host_certificate_renew_composite,
        "summary": "Renew an ESXi host certificate from the vCenter certificate authority.",
        "description": (
            "vCenter gets a signing request from the host, signs it with its own certificate "
            "authority (VMCA) and installs the new certificate. Reads the certificate before "
            "and after the task."
        ),
        "parameter_schema": CERTIFICATE_RENEW_PARAMETER_SCHEMA,
        "response_schema": CERTIFICATE_RENEW_RESPONSE_SCHEMA,
        "group_key": HOST_CERTIFICATES_GROUP_KEY,
        "tags": list(_TAGS),
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": CERTIFICATE_RENEW_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _host_certificate.OP_ID_HOST_CERTIFICATE_CSR,
        "handler": _host_certificate.host_certificate_csr_composite,
        "summary": "Make a certificate signing request (CSR) on an ESXi host.",
        "description": (
            "The host makes a new private key and returns a signing request in PEM. The key "
            "stays on the host, and the current certificate stays in use until an install."
        ),
        "parameter_schema": CERTIFICATE_CSR_PARAMETER_SCHEMA,
        "response_schema": CERTIFICATE_CSR_RESPONSE_SCHEMA,
        "group_key": HOST_CERTIFICATES_GROUP_KEY,
        "tags": list(_TAGS),
        "safety_level": "caution",
        "requires_approval": True,
        "llm_instructions": CERTIFICATE_CSR_LLM_INSTRUCTIONS,
    },
    {
        "op_id": _host_certificate.OP_ID_HOST_CERTIFICATE_INSTALL,
        "handler": _host_certificate_install.host_certificate_install_composite,
        "summary": "Install a CA-signed certificate on an ESXi host, with its CA chain.",
        "description": (
            "Adds the CA chain to the CA certificates the host trusts, installs the signed "
            "certificate and tells the host services to use it. Reads the certificate back. "
            "Never takes a private key."
        ),
        "parameter_schema": CERTIFICATE_INSTALL_PARAMETER_SCHEMA,
        "response_schema": CERTIFICATE_INSTALL_RESPONSE_SCHEMA,
        "group_key": HOST_CERTIFICATES_GROUP_KEY,
        "tags": list(_TAGS),
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": CERTIFICATE_INSTALL_LLM_INSTRUCTIONS,
    },
)

#: VI-JSON vendor calls of each change (``_VIM_SUB_OPS_*`` naming, so the
#: vcenter.yaml sweep skips them; the vi-json reconcile lanes check them).
_VIM_SUB_OPS_HOST_CERTIFICATE_RENEW: Final = (
    _OP_RETRIEVE_PROPERTIES,
    _host_certificate.OP_RETRIEVE_SERVICE_CONTENT,
    _host_certificate.OP_REFRESH_CERTIFICATES,
)
_VIM_SUB_OPS_HOST_CERTIFICATE_CSR: Final = (
    _OP_RETRIEVE_PROPERTIES,
    _host_certificate.OP_GENERATE_CSR,
)
_VIM_SUB_OPS_HOST_CERTIFICATE_INSTALL: Final = (
    _OP_RETRIEVE_PROPERTIES,
    _host_certificate.OP_LIST_CA_CERTIFICATES,
    _host_certificate.OP_LIST_CA_CRLS,
    _host_certificate.OP_REPLACE_CA_CERTIFICATES,
    _host_certificate.OP_INSTALL_SERVER_CERTIFICATE,
    _host_certificate.OP_NOTIFY_AFFECTED_SERVICES,
)

#: composite op id -> the vendor calls it may make.
HOST_CERTIFICATE_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _host_certificate.OP_ID_HOST_CERTIFICATE_RENEW: _VIM_SUB_OPS_HOST_CERTIFICATE_RENEW,
    _host_certificate.OP_ID_HOST_CERTIFICATE_CSR: _VIM_SUB_OPS_HOST_CERTIFICATE_CSR,
    _host_certificate.OP_ID_HOST_CERTIFICATE_INSTALL: _VIM_SUB_OPS_HOST_CERTIFICATE_INSTALL,
}
