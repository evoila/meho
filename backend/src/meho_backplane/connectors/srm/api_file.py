# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The API description files ``srm.api_file.get`` can read.

Broadcom does not publish the SRM 8.8 and Live Site Recovery 9.0 API
files as downloads; a running appliance serves them. ``srm.api_file.get``
reads one of two files (input ``file``):

* ``srm`` -- ``GET /api/rest/srm/v2/open-api.yaml``, the Site Recovery
  API;
* ``configure`` -- ``GET /api/rest/configure/v2/open-api.yaml``, the
  appliance setup API.

The read itself (bounded fetch, secret check, parts, size check) is
shared with ``vr.api_file.get`` and lives in
:mod:`meho_backplane.connectors._shared.dr_api_file`.
"""

from __future__ import annotations

__all__ = ["API_FILES"]

#: The files the read can fetch, by input name.
API_FILES: dict[str, str] = {
    "srm": "/api/rest/srm/v2/open-api.yaml",
    "configure": "/api/rest/configure/v2/open-api.yaml",
}
