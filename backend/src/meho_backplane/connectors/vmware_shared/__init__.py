# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared code that only the VMware connectors use.

This folder is not a connector. It registers nothing, and importing it
has no side effects. The start-up walk imports it like every other
folder under ``connectors/``, and that import does nothing.

Modules:

* :mod:`meho_backplane.connectors.vmware_shared.vcfa_session` — the VCF
  Automation session login. The ``vro`` connector uses it.
* :mod:`meho_backplane.connectors.vmware_shared.dr_rest`,
  :mod:`~meho_backplane.connectors.vmware_shared.dr_api_file` and
  :mod:`~meho_backplane.connectors.vmware_shared.dr_rest_schemas` — the
  base class, the API file read and the response schemas that the
  ``srm`` and ``vr`` connectors share.
* :mod:`meho_backplane.connectors.vmware_shared.vcfa_integration_mixin` and
  the modules next to it (``vcfa_integration_*``, ``vcfa_integrations``,
  ``vcfa_git_sources``, ``vcfa_ipam_package``) — the integration ops
  (Orchestrator, IPAM, Git) that the ``vcf_automation`` and ``vra8``
  connectors share. Importing them registers the previews of the two
  destructive ops.
* :mod:`meho_backplane.connectors.vmware_shared.license_key` — the one
  rule for showing a licence key: only its last 5 characters stay
  visible. The ``sddc_manager``, ``vcf_operations`` and ``vmware_rest``
  connectors use it.

Shared code that is not VMware-only belongs in
:mod:`meho_backplane.connectors._shared`. Code here may import
``_shared``; ``_shared`` must not import this folder.
"""
