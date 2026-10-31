# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The group text of the infrastructure ops (no imports, so ``typed_ops`` can use it)."""

from __future__ import annotations

__all__ = ["infra_when_to_use"]


def infra_when_to_use(prefix: str, product: str, *, with_vra8_only: bool) -> str:
    """The group text: which reads and changes the group holds."""
    zone_read = f"cloud zones ({prefix}.zone.list), " if with_vra8_only else ""
    zone_note = "" if with_vra8_only else f" Cloud zones are listed with {prefix}.zone.list."
    project_create = f"create a project ({prefix}.project.create), " if with_vra8_only else ""
    return (
        f"Cloud accounts, cloud zones, profiles and project members of {product}. Reads: "
        f"cloud accounts with their regions and state ({prefix}.cloud_account.list), "
        f"{zone_read}flavor profiles ({prefix}.flavor_profile.list), network profiles with "
        f"their networks and IP ranges ({prefix}.network_profile.list) and storage profiles "
        f"({prefix}.storage_profile.list). Changes: add a vCenter or NSX cloud account "
        f"({prefix}.cloud_account.create), remove one ({prefix}.cloud_account.delete; only a "
        f"person, after a preview), create or change a cloud zone ({prefix}.zone.set) or a "
        f"flavor profile ({prefix}.flavor_profile.set), {project_create}and add or remove "
        f"project members ({prefix}.project.members.set). A person must approve each change."
        f"{zone_note}"
    )
