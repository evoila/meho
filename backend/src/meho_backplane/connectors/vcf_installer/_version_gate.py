# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stop an Installer operation early when the target's version lacks its call.

The connector serves VCF Installer 9.0 and 9.1 (``>=9.0,<10.0``). A few
vendor calls exist only from 9.1 on, for example the resource calculation.
An operation that needs such a call runs :func:`require_installer_version`
first. On an older target it raises :exc:`NotImplementedError` before any
vendor call; the dispatcher turns that into an audited
``connector_unsupported`` error with the message in ``extras.detail``.

A target with no detected version, or a version that does not parse, goes on:
the vendor's own error is then classified as usual.
"""

from __future__ import annotations

from typing import Any

from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.resolver import resolve_target_version

__all__ = ["installer_version", "require_installer_version"]


def installer_version(target: Any) -> Version | None:
    """The target's Installer version, or ``None`` when unknown or not parseable."""
    raw = resolve_target_version(target)
    if raw is None:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def require_installer_version(target: Any, *, op_id: str, minimum: str, feature: str) -> None:
    """Raise :exc:`NotImplementedError` when *target* runs an Installer older than *minimum*.

    The message names the operation, the target's version and the versions
    that have the call, for example ``"installer.sddc.resources.calculate:
    the resource calculation needs VCF Installer 9.1 or newer; this target
    is 9.0.2."``.
    """
    version = installer_version(target)
    if version is None or version >= Version(minimum):
        return
    raise NotImplementedError(
        f"{op_id}: {feature} needs VCF Installer {minimum} or newer; "
        f"this target is {resolve_target_version(target)}."
    )
