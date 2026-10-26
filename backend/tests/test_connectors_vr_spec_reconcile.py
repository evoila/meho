# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vr spec-reconcile lane -- hand-coded vSphere Replication paths vs the pinned vendor file.

Every vendor call the vr connector makes is listed here with its method:

* the login, ``POST /api/rest/vr/v2/session``;
* the server summary, ``GET /api/rest/vr/v2/info`` (``vr.about`` and the
  version read);
* the two API files ``vr.api_file.get`` reads
  (``/api/rest/vr/v2/open-api.yaml`` and
  ``/api/rest/configure/v2/open-api.yaml``) -- pinned in the manifest
  test and left out of the served check: the appliance serves them as
  files, the vendor file does not list them.

The vendor file lists its paths relative to ``/api/rest/vr/v2``, so the
lane strips that base before it compares. The 9.1 file
(``vcf-protection-and-recovery-9.1/replication-vr-v2.yaml``) is pinned on
the spec shelf. The 8.8 and 9.0 files are added to :data:`_SPEC_FILES`
once the lab pins them with ``vr.api_file.get``.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vr import VrConnector
from meho_backplane.connectors.vr.connector import API_FILES
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

#: One ``(shelf directory, file)`` per vSphere Replication version with a pinned file.
_SPEC_FILES = (("vcf-protection-and-recovery-9.1", "replication-vr-v2.yaml"),)


def _declared_api_op_ids() -> set[str]:
    """The hand-coded API calls, relative to the API base, as ``METHOD:/path``."""
    base = VrConnector.api_base
    calls = {"POST": VrConnector.session_path, "GET": VrConnector.info_path}
    return {f"{method}:{path.removeprefix(base)}" for method, path in calls.items()}


def test_vr_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift cannot silently shrink the reconcile."""
    assert _declared_api_op_ids() == {"POST:/session", "GET:/info"}
    assert set(API_FILES.values()) == {
        "/api/rest/vr/v2/open-api.yaml",
        "/api/rest/configure/v2/open-api.yaml",
    }


@pytest.mark.parametrize(("spec_dir", "spec_file"), _SPEC_FILES)
def test_vr_hand_coded_api_paths_are_served_by_the_pinned_file(
    spec_dir: str, spec_file: str
) -> None:
    """Every hand-coded vSphere Replication API call is served by the pinned vendor file."""
    spec_path = require_shelf_spec(spec_dir, spec_file)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_api_op_ids(), served, spec_label=f"{spec_dir}/{spec_file}")
