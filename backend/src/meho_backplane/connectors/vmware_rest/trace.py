# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the flight recorder may store of a vCenter JSON answer.

The shared redaction of the flight recorder knows secrets by shape (a token,
a key) and by op family (a login). The value of a VM advanced setting is
neither: a cloud-init user-data blob under ``guestinfo.userdata`` looks like
any other string. So :meth:`VmwareRestConnector.trace_response_body` runs
every JSON answer through :func:`trace_payload` before the trace stores it.

The stored answer keeps every field, except the ``value`` of a key and value
pair (an ``OptionValue`` of ``config.extraConfig``, or any other vSphere
``{key, value}`` row) whose key may hold a secret by the rule the operations
use (:func:`~meho_backplane.connectors.vmware_rest.typed_ops_vm_advanced_settings.is_secret_key`).
That value is replaced by :data:`SECRET_SETTING_MARKER`. The shared redaction
still runs on the rest of the body.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.typed_ops_vm_advanced_settings import is_secret_key

__all__ = ["SECRET_SETTING_MARKER", "trace_payload"]

#: What the stored trace holds in place of a setting value that may hold a secret.
SECRET_SETTING_MARKER: Final = "[MEHO-REDACTED:advanced-setting-value]"


def trace_payload(payload: Any) -> Any:
    """Return *payload* with the value of every secret-named key and value pair masked."""
    if isinstance(payload, dict):
        key = payload.get("key")
        if "value" in payload and isinstance(key, str) and is_secret_key(key):
            return {**payload, "value": SECRET_SETTING_MARKER}
        return {name: trace_payload(item) for name, item in payload.items()}
    if isinstance(payload, list):
        return [trace_payload(item) for item in payload]
    return payload
