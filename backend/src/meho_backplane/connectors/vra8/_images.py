# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler methods of the Aria Automation 8 image ops (metadata in :mod:`.image_ops`).

Aria Automation 8 and VCF Automation 9 speak the same ``/iaas/api`` calls
for image profiles, fabric images and the image enumeration. So these
methods run the shared handlers of
:mod:`meho_backplane.connectors.vcf_automation._images` with the
``vcfa.vra8`` prefix, so the guidance names the ``vcfa.vra8.*`` ops.
:class:`~meho_backplane.connectors.vra8.connector.Vra8Connector` inherits
this mixin; its one bearer works on ``/iaas/api`` like on every other
vRA 8 service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.adapters.http import HttpConnector
    from meho_backplane.connectors.vra8.session import Vra8TargetLike

__all__ = ["VRA8_IMAGE_PREFIX", "Vra8ImageOpsMixin"]

#: The op id prefix of the vRA 8 image ops.
VRA8_IMAGE_PREFIX: Final[str] = "vcfa.vra8"


class Vra8ImageOpsMixin:
    """The four image op handlers, for :class:`~.connector.Vra8Connector`."""

    async def image_profile_list(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.image_profile.list`` -- ``GET /iaas/api/image-profiles``."""
        from meho_backplane.connectors.vcf_automation._images import tenant_image_profile_list

        return await tenant_image_profile_list(
            cast("HttpConnector", self), operator, target, params
        )

    async def fabric_image_list(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.fabric_image.list`` -- ``GET /iaas/api/fabric-images``."""
        from meho_backplane.connectors.vcf_automation._images import tenant_fabric_image_list

        return await tenant_fabric_image_list(cast("HttpConnector", self), operator, target, params)

    async def cloud_account_enumerate_images(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.cloud_account.enumerate_images`` -- start, then wait until it ends."""
        from meho_backplane.connectors.vcf_automation._images import (
            tenant_cloud_account_enumerate_images,
        )

        return await tenant_cloud_account_enumerate_images(
            cast("HttpConnector", self), operator, target, params, prefix=VRA8_IMAGE_PREFIX
        )

    async def image_profile_mapping_update(
        self, operator: Operator, target: Vra8TargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vcfa.vra8.image_profile.mapping.update`` -- change one name, keep the others."""
        from meho_backplane.connectors.vcf_automation._images import (
            tenant_image_profile_mapping_update,
        )

        return await tenant_image_profile_mapping_update(
            cast("HttpConnector", self), operator, target, params, prefix=VRA8_IMAGE_PREFIX
        )
