# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_installer — InstallerConnector package.

Importing this package registers :class:`InstallerConnector` against the v2
connector registry under ``(product="installer", version="9.1",
impl_id="installer-rest")``. The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which walks
every ``connectors/<product>/`` subpackage at startup, so the registration lands
before any dispatch can occur.

The bring-up status poll (``installer.sddc.status``) ships as a typed op
(``source_kind="typed"``) via :mod:`.typed_ops` / :mod:`.typed_reads`,
dispatchable on a fresh boot with zero catalog ingest. The governed bring-up
write (validate → deploy → poll) arrives as a ``dangerous`` + ``requires_approval``
composite in a following increment.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
from meho_backplane.connectors.vcf_installer.profile import INSTALLER_EXECUTION_PROFILE
from meho_backplane.connectors.vcf_installer.session import (
    InstallerCredentialsLoader,
    InstallerTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.vcf_installer.typed_ops import (
    INSTALLER_TYPED_OPS,
    InstallerTypedOp,
    register_installer_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the VCF Installer connector — the
#: dispatch-canonical ``(product, version, impl_id)`` triple
#: :func:`parse_connector_id` derives from ``"installer-rest-9.1"``.
INSTALLER_PRODUCT: Final[str] = "installer"
INSTALLER_VERSION: Final[str] = "9.1"
INSTALLER_IMPL_ID: Final[str] = "installer-rest"
INSTALLER_CONNECTOR_ID: Final[str] = f"{INSTALLER_IMPL_ID}-{INSTALLER_VERSION}"

register_connector_v2(
    product="installer",
    version="9.1",
    impl_id="installer-rest",
    cls=InstallerConnector,
)

# Wildcard fallback: a target with ``version=None`` (fresh, unfingerprinted)
# resolves to this connector through the resolver's ``versioned_over_wildcard``
# step rather than 501-ing with ``no_connector``. The versioned entry above
# always wins when both are present.
register_connector_v2(
    product="installer",
    version="",
    impl_id="",
    cls=InstallerConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
register_typed_op_registrar(register_installer_typed_operations)

__all__ = [
    "INSTALLER_CONNECTOR_ID",
    "INSTALLER_EXECUTION_PROFILE",
    "INSTALLER_IMPL_ID",
    "INSTALLER_PRODUCT",
    "INSTALLER_TYPED_OPS",
    "INSTALLER_VERSION",
    "InstallerConnector",
    "InstallerCredentialsLoader",
    "InstallerTargetLike",
    "InstallerTypedOp",
    "SessionCredentials",
    "load_credentials_from_vault",
    "register_installer_typed_operations",
]
