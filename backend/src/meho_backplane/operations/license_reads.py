# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested reads of licence keys stay off.

A vendor API file can describe a read whose answer carries the product's
licence keys unmasked: SDDC Manager's ``GET /v1/license-keys``, the vCenter
licence manager's ``licenses`` property, an NSX ``/licenses`` list. The
ingest gives such a read its risk level from the HTTP method, so it would
be a ``safe`` read that runs at once. A licence key is a paid secret, so
MEHO never runs it:

* ``enable-reads`` skips it (``bulk_enable_read_ops`` in
  :mod:`meho_backplane.operations.ingest._internals`);
* a call or a preview of it answers ``license_read_off`` before anything
  runs, also when an operator turned the row on by hand
  (:func:`~meho_backplane.operations._errors.result_license_read_off`).
  A refused call leaves one ``policy_decision`` audit row with the op id
  and the code (:mod:`meho_backplane.operations._license_refusal`).

The hand-written licence reads of a connector are the way to read
licences: they show each key masked.

The rule is one word in the path, so it needs no list of vendor paths:
a ``GET`` or ``HEAD`` whose path mentions licences (``license-keys``,
``licenses``, ``licensing``, ``licence``) is a licence read. That is broad
by design, in the same direction as the flight recorder's family rule:
an ingested read that names licences and carries no key stays off too,
and a hand-written read covers it when it is needed.

This module imports nothing from MEHO, so the ingest layer and the
dispatcher can both use it without a cycle.
"""

from __future__ import annotations

from typing import Final

__all__ = ["LICENSE_PATH_MARK", "LICENSE_READ_METHODS", "is_license_read", "is_license_read_op_id"]

#: The word that marks a licence path, matched case-insensitively. It is
#: the stem of ``license``, ``licenses``, ``licensing`` and ``licence``.
LICENSE_PATH_MARK: Final[str] = "licen"

#: The HTTP methods of a read. Same set as the ingest's read class.
LICENSE_READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})


def is_license_read(method: str | None, path: str | None) -> bool:
    """True when *method* and *path* describe an ingested read of licences."""
    if method is None or path is None:
        return False
    return method.upper() in LICENSE_READ_METHODS and LICENSE_PATH_MARK in path.lower()


def is_license_read_op_id(op_id: str) -> bool:
    """True when the ingested op id (``METHOD:/path``) is a read of licences.

    A hand-written op id (``sddc.license.list``) has no method prefix and
    never matches.
    """
    method, sep, path = op_id.partition(":")
    if not sep:
        return False
    return is_license_read(method, path)
