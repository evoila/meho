# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""All eleven cloud template and catalog ops, for both Automation connectors.

See :mod:`.vcfa_content` for how one op list serves two connectors.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_shared.vcfa_catalog_ops import CATALOG_OPS
from meho_backplane.connectors.vmware_shared.vcfa_content import ContentOpSpec
from meho_backplane.connectors.vmware_shared.vcfa_template_ops import TEMPLATE_OPS

__all__ = ["CONTENT_OPS"]

#: The template ops, then the catalog ops.
CONTENT_OPS: Final[tuple[ContentOpSpec, ...]] = (*TEMPLATE_OPS, *CATALOG_OPS)
