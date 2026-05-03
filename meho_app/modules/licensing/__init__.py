# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""License-issuance audit log.

Distinct from :mod:`meho_app.core.licensing`, which is the verifier path
read at startup. This module is the write path: every signed enterprise
token minted by ``scripts/issue-license.py`` (Initiative #505 Task #519,
not yet shipped) is recorded here for compliance and forensics.
"""
