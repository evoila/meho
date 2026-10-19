# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingest-time summaries for the VMware REST catalogs (#4329).

The vSphere REST spec gives no ``summary`` to any operation, and the VI-JSON
spec repeats one summary for the same method on many object types. The
connector correction in ``connectors/vmware_rest/ingest_corrections.py``
fills a missing summary from the description and puts the object type in
front of a shared VI-JSON summary. The specs here are small and hand-made.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.ingest_corrections import (
    MAX_SUMMARY_CHARS,
    summary_from_description,
)
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.register_ingested import _prepare_ingested_operations
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.ingest.spec_corrections import apply_spec_corrections

_LONG_FIRST_SENTENCE = (
    "Returns the details of every backup job that ran on this appliance, "
    "including the status, the size, the start time, the end time and the "
    "location of each job."
)

_REST_SPEC = f"""
openapi: 3.0.3
info:
  title: summary fixture
  version: 9.0.0.0
servers:
  - url: https://{{host}}/api
    variables:
      host:
        default: localhost
paths:
  /vcenter/cluster:
    get:
      operationId: list
      description: |-
        Returns information about at most 1000 visible clusters in vCenter
        matching the filter. Use the filter to narrow the list.

        This operation was added in __vSphere API 7.0.0.0__.
      responses:
        "200":
          description: Clusters.
  /appliance/health/settings:
    patch:
      operationId: set
      description: |-
        Set health settings based on provided healthSpec

        This operation was added in __vSphere API 7.0.0.1__.
      responses:
        "204":
          description: Done.
  /appliance/recovery/backup/job/details:
    get:
      operationId: details
      description: "{_LONG_FIRST_SENTENCE} More text."
      responses:
        "200":
          description: Jobs.
  /vcenter/host:
    get:
      operationId: listHosts
      summary: List the hosts.
      description: Returns information about at most 2500 visible hosts. More text.
      responses:
        "200":
          description: Hosts.
"""

_VI_JSON_SPEC = """
openapi: 3.0.3
info:
  title: vi-json fixture
  version: 9.0.0.0
servers:
  - url: https://{vcenter-host}/sdk/vim25/{release}
    variables:
      vcenter-host:
        default: localhost
      release:
        default: 9.0.0.0
paths:
  /VirtualMachine/{moId}/Destroy_Task:
    post:
      operationId: VirtualMachineDestroy_Task
      summary: |
        Destroys this object, deleting its contents and removing it from its parent
        folder (if any).
      parameters:
        - name: moId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Task.
  /Folder/{moId}/Destroy_Task:
    post:
      operationId: FolderDestroy_Task
      summary: |
        Destroys this object, deleting its contents and removing it from its parent
        folder (if any).
      parameters:
        - name: moId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Task.
  /vsan/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary:
    post:
      operationId: VsanQueryVcClusterHealthSummary
      summary: Queries the vSAN cluster health summary.
      parameters:
        - name: moId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Summary.
  /HostSnmpSystem/{moId}/SendTestNotification:
    post:
      operationId: SendTestNotification
      description: |
        ***Required privileges:*** Global.Settings
      parameters:
        - name: moId
          in: path
          required: true
          schema:
            type: string
      responses:
        "204":
          description: Done.
"""


def _corrected(spec: str, version: str = "9.0") -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("spec:fixture", spec_source="spec:fixture.yaml", content=spec)
    corrected = apply_spec_corrections(
        product="vmware", version=version, impl_id="vmware-rest", protos=rows
    )
    return {row.op_id: row for row in corrected}


def test_rest_op_without_summary_gets_first_sentence_of_description() -> None:
    rows = _corrected(_REST_SPEC)
    summary = rows["GET:/vcenter/cluster"].summary
    assert summary == (
        "Returns information about at most 1000 visible clusters in vCenter matching the filter."
    )
    assert len(summary) <= MAX_SUMMARY_CHARS
    assert summary.endswith(".")


def test_first_paragraph_without_period_is_the_summary() -> None:
    rows = _corrected(_REST_SPEC)
    assert (
        rows["PATCH:/appliance/health/settings"].summary
        == "Set health settings based on provided healthSpec"
    )


def test_long_first_sentence_is_cut_at_a_word() -> None:
    rows = _corrected(_REST_SPEC)
    summary = rows["GET:/appliance/recovery/backup/job/details"].summary
    assert summary is not None
    assert len(_LONG_FIRST_SENTENCE) > MAX_SUMMARY_CHARS
    assert len(summary) <= MAX_SUMMARY_CHARS
    assert summary.endswith("…")
    assert _LONG_FIRST_SENTENCE.startswith(summary.removesuffix("…"))


def test_op_with_its_own_summary_keeps_it() -> None:
    rows = _corrected(_REST_SPEC)
    assert rows["GET:/vcenter/host"].summary == "List the hosts."
    vi_rows = _corrected(_VI_JSON_SPEC)
    op_id = "POST:/vsan/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary"
    assert vi_rows[op_id].summary == "Queries the vSAN cluster health summary."


def test_shared_vi_json_summaries_name_the_object_type() -> None:
    rows = _corrected(_VI_JSON_SPEC)
    vm = rows["POST:/VirtualMachine/{moId}/Destroy_Task"].summary
    folder = rows["POST:/Folder/{moId}/Destroy_Task"].summary
    text = (
        "Destroys this object, deleting its contents and removing it from its parent "
        "folder (if any)."
    )
    assert vm == f"VirtualMachine: {text}"
    assert folder == f"Folder: {text}"
    assert vm != folder


def test_metadata_only_description_gives_no_summary() -> None:
    rows = _corrected(_VI_JSON_SPEC)
    assert rows["POST:/HostSnmpSystem/{moId}/SendTestNotification"].summary is None
    assert summary_from_description("***Since:*** vSphere API Release 7.0.3.0\n") is None
    assert summary_from_description(None) is None


def test_corrections_apply_on_every_vmware_rest_version() -> None:
    rows = _corrected(_REST_SPEC, version="8.0")
    assert rows["GET:/vcenter/cluster"].summary is not None


def test_register_path_applies_the_summary_corrections() -> None:
    rows = parse_openapi("spec:fixture", spec_source="spec:fixture.yaml", content=_VI_JSON_SPEC)
    prepared = _prepare_ingested_operations(
        rows, product="vmware", version="9.0", impl_id="vmware-rest"
    )
    by_id = {row.op_id: row for row in prepared}
    assert by_id["POST:/Folder/{moId}/Destroy_Task"].summary is not None
    assert by_id["POST:/Folder/{moId}/Destroy_Task"].summary.startswith("Folder: ")


def test_other_products_are_left_alone() -> None:
    rows = parse_openapi("spec:fixture", spec_source="spec:fixture.yaml", content=_VI_JSON_SPEC)
    corrected = apply_spec_corrections(
        product="other", version="1.0", impl_id="other-rest", protos=rows
    )
    assert [row.summary for row in corrected] == [row.summary for row in rows]
