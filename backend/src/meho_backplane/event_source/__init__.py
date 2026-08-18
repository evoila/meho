# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``event_source`` registry: tenant-scoped external event producers.

Initiative #2877 (G11.3 inbound ingest), Task #2880 (T3). This package
owns the registry the inbound webhook path (#2881, out of scope here)
resolves a JWT-less sender against: its Pydantic schemas, its name/slug
resolvers, and the Vault secret-custody helpers that write a source's
auth secret to Vault while the DB row keeps only the path.

The ORM model lives in :mod:`meho_backplane.db.models` (``EventSource``);
the REST admin surface in :mod:`meho_backplane.api.v1.event_source`.
"""
