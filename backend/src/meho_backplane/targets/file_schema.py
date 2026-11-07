# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shape of one record in a targets file, as a published JSON Schema.

The toolkit repos keep a ``targets.yaml`` file: a ``targets:`` list with one
record per system. ``meho targets import`` reads that file. This module
describes one record, so both sides work from one description. The same
schema is checked in as ``docs/schemas/target-record-v1.schema.json``.
``tests/test_targets_file_schema.py`` fails when this model changes and the
file was not made again.

The model is for the schema file and for tests. The import does not use it:
the import maps keys itself (``cli/internal/cmd/targets/import.go``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.targets.schemas import SecretFieldName

#: The number in the schema file name.
TARGET_RECORD_SCHEMA_VERSION = "1"


class FingerprintRecord(BaseModel):
    """The detected product and version of a system.

    MEHO fills this in when it probes the target. A file may carry it, but
    the import skips it with a warning and never writes it.
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str
    product: str
    version: str | None = None
    build: str | None = None
    edition: str | None = None
    reachable: bool
    probed_at: datetime
    probe_method: str
    extras: dict[str, Any] = Field(default_factory=dict)


class TargetRecord(BaseModel):
    """One entry of the ``targets:`` list.

    A key that is not listed here is allowed. The import keeps it in the
    target's ``extras`` and prints a warning.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=200, description="The target's name.")
    aliases: list[str] = Field(default_factory=list)
    product: str = Field(min_length=1, max_length=100, description="The product token.")
    version: str | None = Field(
        default=None,
        max_length=100,
        description='The product version you state. Write it as text, for example "8.0".',
    )
    host: str = Field(min_length=1, max_length=512, description="The host or IP address.")
    port: int | None = Field(default=None, ge=1, le=65535)
    fqdn: str | None = None
    secret_ref: str | None = Field(default=None, description="A path in the credential store.")
    auth_model: AuthModel = AuthModel.SHARED_SERVICE_ACCOUNT
    vpn_required: bool = False
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = Field(default=None, max_length=512)
    secret_field_map: dict[SecretFieldName, SecretFieldName] = Field(
        default_factory=dict,
        description="Maps a connector field to the field name in your secret. Names only.",
    )
    extras: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    preferred_impl_id: str | None = Field(default=None, max_length=200)
    flight_recorder_capture: bool | None = None
    fingerprint: FingerprintRecord | None = Field(
        default=None,
        description="Detected product and version. The import skips it.",
    )


def target_record_json_schema() -> dict[str, Any]:
    """The JSON Schema of one target record."""
    schema = TargetRecord.model_json_schema()
    schema["title"] = "Target record"
    schema["$id"] = (
        f"https://github.com/evoila/meho/blob/main/docs/schemas/"
        f"target-record-v{TARGET_RECORD_SCHEMA_VERSION}.schema.json"
    )
    return schema
