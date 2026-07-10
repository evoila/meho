# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.github.composites -- gh-rest composites.

Side-effect import: this package's ``__init__`` queues
:func:`register_github_composite_operations` onto the lifespan-driven
registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`.

The chassis lifespan's
:func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
invokes every registered registrar in registration order after
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
has walked every ``connectors/<product>/`` subpackage, so the
``endpoint_descriptor`` upsert for the T4 composite lands before any
dispatch can fire.

Layout mirrors the vmware-rest composites package: ``__init__`` wires
the registrar; ``_register.py`` carries per-composite registration
metadata; ``_read.py`` carries handler implementations; ``schemas.py``
carries the JSON Schema 2020-12 parameter + response contracts.

Scope at T4 (#1224): 1 read composite --
``gh.composite.pr_status_summary``. Future T7+ Tasks add write
composites under this package.
"""

from meho_backplane.connectors.github.composites._read import (
    pr_status_summary_composite,
)
from meho_backplane.connectors.github.composites._register import (
    register_github_composite_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the composite-op upsert onto the lifespan-driven registrar list.
# The lifespan calls ``run_typed_op_registrars`` after
# ``_eager_import_connectors`` so every connector subpackage has self-
# registered by the time the runner iterates.
register_typed_op_registrar(register_github_composite_operations)

__all__ = [
    "pr_status_summary_composite",
    "register_github_composite_operations",
]
