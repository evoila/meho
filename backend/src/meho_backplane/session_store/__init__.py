# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent session store: upload, index, object storage, retention and roles.

A person uploads one agent session (a transcript and its folder) as one
compressed archive. MEHO checks every file for secrets, keeps the result in
S3-compatible object storage and writes one index row per session. See
``docs/codebase/session-store.md``.
"""
