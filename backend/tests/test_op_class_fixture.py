# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every known operation keeps the op class it had on 1 November 2026.

``tests/snapshots/op_class_fixture.json`` is a snapshot: the answer of
:func:`~meho_backplane.broadcast.events.classify_op` for every operation
that was registered on that day, plus every op id the classifier pinned
by name. It was taken before the per-connector class declarations
(``connectors/<name>/op_classes.py``) replaced the shared lists in
``broadcast/events.py``. The test here checks that the move changed no
class.

The fixture is frozen on purpose. A new operation does not go in here:
it declares its class in its own connector, and the class of a new
operation is checked by ``tests/test_operation_op_class.py`` and
``tests/test_broadcast_classifier_coverage.py``. Only a conscious change
of an existing class edits this file.

The file lives under ``tests/snapshots/``, not ``tests/data/``: it is the
answer of MEHO's own classifier, not a recorded vendor answer, and the
saved-capture secret scan (``test_capture_secret_scan.py``) would flag op
ids such as ``keycloak.user.reset_password`` as a secret-named field.

To take the snapshot again (for example after such a change), run from
``backend/``::

    uv run python -c "import json, sys; sys.path.insert(0, 'tests'); \\
    from test_broadcast_classifier_coverage import _capture_registered_ops; \\
    from meho_backplane.broadcast.events import classify_op; \\
    old = json.load(open('tests/snapshots/op_class_fixture.json')); \\
    ids = set(old) | {op for op, _ in _capture_registered_ops()}; \\
    json.dump({op: classify_op(op) for op in sorted(ids)}, \\
    open('tests/snapshots/op_class_fixture.json', 'w'), indent=1, sort_keys=True)"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.registry import _eager_import_connectors

_FIXTURE = Path(__file__).parent / "snapshots" / "op_class_fixture.json"


@pytest.fixture(scope="module")
def expected_classes() -> dict[str, str]:
    """The snapshot, with every connector package imported first.

    A connector declares the class of its operations when its package is
    imported, as the server does at start. Without the import the
    classifier knows only the core pins and the pattern rules.
    """
    _eager_import_connectors()
    with _FIXTURE.open(encoding="utf-8") as handle:
        loaded: dict[str, str] = json.load(handle)
    return loaded


def test_fixture_is_not_empty(expected_classes: dict[str, str]) -> None:
    assert len(expected_classes) > 900


def test_every_known_operation_keeps_its_class(expected_classes: dict[str, str]) -> None:
    changed = {
        op_id: (expected, classify_op(op_id))
        for op_id, expected in expected_classes.items()
        if classify_op(op_id) != expected
    }
    assert not changed, (
        "classify_op() changed its answer for these op ids (op id: (before, now)). "
        "If the change is wanted, update tests/data/op_class_fixture.json in the "
        f"same pull request: {changed}"
    )
