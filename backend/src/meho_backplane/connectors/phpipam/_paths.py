# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The phpIPAM REST paths MEHO calls, and the reference list they come from.

phpIPAM publishes its API as a web page, not as an OpenAPI file, so there is
no spec to check these paths against. Each path below is pinned to the
phpIPAM 1.8.2 API reference and to the 1.8.2 source of the API controller
that serves it:

* API reference: https://phpipam.net/api/api_reference/
* API documentation (login, answer shape): https://phpipam.net/api/api_documentation/
* Source (tag ``v1.8.2``): ``api/controllers/Subnets.php``,
  ``api/controllers/Addresses.php``, ``api/controllers/User.php``,
  ``api/controllers/Responses.php`` in https://github.com/phpipam/phpipam

The calls, with what phpIPAM answers (checked in the 1.8.2 source):

* ``POST /api/{app_id}/user/`` -- user login with HTTP Basic. ``data.token``
  is the token; MEHO sends it as the ``phpipam-token`` header after that.
* ``GET /api/{app_id}/subnets/`` -- all subnets of all sections.
* ``GET /api/{app_id}/subnets/cidr/{subnet}/{mask}/`` -- the subnets with this
  CIDR; 404 when there is none.
* ``GET /api/{app_id}/subnets/{subnet_id}/`` -- one subnet; 400 when the id
  is unknown.
* ``GET /api/{app_id}/subnets/{subnet_id}/usage/`` -- how full the subnet is
  (``used``, ``freehosts``, ``maxhosts``).
* ``GET /api/{app_id}/subnets/{subnet_id}/slaves/`` -- the child subnets; 404
  when there is none.
* ``GET /api/{app_id}/subnets/{subnet_id}/addresses/`` -- the used addresses;
  404 when there is none.
* ``GET /api/{app_id}/subnets/{subnet_id}/addresses/{ip}/`` -- one address in
  one subnet; 404 when the address is not reserved. MEHO does not read an
  address with ``GET /api/{app_id}/addresses/{ip}/{subnet_id}/``: in 1.8.2 that
  call finds only the first address row of a subnet, because the ``ip`` field
  of the other rows is never filled in there.
* ``GET /api/{app_id}/addresses/first_free/{subnet_id}/`` -- the first free
  address (used by the approval preview only).
* ``POST /api/{app_id}/addresses/`` -- reserve one address with ``subnetId``,
  ``ip``, ``hostname``, ``description`` and ``note``; 201, or 409 when the
  address is taken.
* ``POST /api/{app_id}/addresses/first_free/{subnet_id}/`` -- reserve the first
  free address; 201 with the address in ``data``.
* ``DELETE /api/{app_id}/addresses/{ip}/{subnet_id}/`` -- release one address;
  404 when it is not reserved. MEHO never sends ``remove_dns``.

Every answer has the shape ``{code, success, message?, data?, time?}``.

The templates are relative to the phpIPAM API root. A target whose phpIPAM
runs under a sub-path (``extras.base_path``, for example ``/phpipam``) gets
that prefix in front (:func:`api_path`). The spec check
(``backend/tests/test_connectors_phpipam_spec_reconcile.py``) reads the
``*_PATH`` constants of this module live and pins them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import quote

__all__ = ["api_path", "encode_segment"]

_USER_PATH = "/api/{app_id}/user/"
_SUBNETS_PATH = "/api/{app_id}/subnets/"
_SUBNET_CIDR_PATH = "/api/{app_id}/subnets/cidr/{subnet}/{mask}/"
_SUBNET_PATH = "/api/{app_id}/subnets/{subnet_id}/"
_SUBNET_USAGE_PATH = "/api/{app_id}/subnets/{subnet_id}/usage/"
_SUBNET_SLAVES_PATH = "/api/{app_id}/subnets/{subnet_id}/slaves/"
_SUBNET_ADDRESSES_PATH = "/api/{app_id}/subnets/{subnet_id}/addresses/"
_ADDRESSES_PATH = "/api/{app_id}/addresses/"
_SUBNET_ADDRESS_PATH = "/api/{app_id}/subnets/{subnet_id}/addresses/{ip}/"
_ADDRESS_PATH = "/api/{app_id}/addresses/{ip}/{subnet_id}/"
_FIRST_FREE_PATH = "/api/{app_id}/addresses/first_free/{subnet_id}/"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def encode_segment(value: object) -> str:
    """Percent-encode one path segment, so a value can never add a ``/``."""
    return quote(str(value), safe="")


def api_path(base_path: str, template: str, values: Mapping[str, object]) -> str:
    """Return *template* with every placeholder filled, under *base_path*.

    Every value is encoded as one path segment. A missing or an extra value
    raises :exc:`ValueError`, so a typo never sends a wrong URL.
    """
    placeholders = set(_PLACEHOLDER_RE.findall(template))
    if placeholders != set(values):
        raise ValueError(
            f"phpIPAM path {template!r} needs {sorted(placeholders)}, got {sorted(values)}"
        )
    filled = _PLACEHOLDER_RE.sub(lambda m: encode_segment(values[m.group(1)]), template)
    return f"{base_path}{filled}"
