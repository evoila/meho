# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test-only fixtures kept outside ``backend/alembic/versions/``.

Anything under this package is **never** part of the production
migration sequence — the CI guard at
``scripts/ci/check_migration_compat.py`` scopes itself to
``backend/alembic/versions/**`` (see ``DEFAULT_VERSIONS_DIR`` in that
script and the ``paths:`` filter in
``.github/workflows/migration-compat.yml``), so synthetic destructive
or schema-evolution-narrative migrations placed here cannot trip the
guard or pollute the production revision graph.
"""
