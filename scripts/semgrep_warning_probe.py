# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Temporary probe for the Semgrep CI check. Never merged."""

import hashlib


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()
