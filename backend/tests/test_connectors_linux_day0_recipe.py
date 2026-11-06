# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recipe-shape guard for the linux-ssh day-0 verification recipe (#3362).

The day-0 verification recipe (documented in
``docs/codebase/connectors-linux.md`` and wired into the
consumer-onboarding ``CLAUDE.md`` template) is an ordered sequence of
shipped T1 read ops followed by a cross-connector functional probe. It
ships **no new ops** -- the recipe is documentation over the existing
surface, and this module is its shape guard.

Coverage (per Task #3362 acceptance criteria):

* Every Linux recipe step names a real, shipped, ``safe``, read-only op
  that resolves to a curated operation group
  (:data:`~meho_backplane.connectors.linux.ops.LINUX_WHEN_TO_USE_BY_GROUP`).
* The functional probe (step 7) references the ``net`` diagnostics
  connector's ``net.dns_lookup`` / ``net.ntp_check`` -- real, ``safe``,
  ``probe``-group net ops -- and is **not** a (nonexistent) Linux verb.
* Both docs cite every recipe op id, so the prose contract and the shipped
  surface cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.connectors.linux import LINUX_OPS
from meho_backplane.connectors.linux.ops import LINUX_WHEN_TO_USE_BY_GROUP

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_LINUX_DOC: Path = _REPO_ROOT / "docs" / "codebase" / "connectors-linux.md"
_CONSUMER_TEMPLATE: Path = _REPO_ROOT / "docs" / "examples" / "consumer-onboarding" / "CLAUDE.md"

#: The ordered day-0 recipe: the six T1 read ops, cheapest-and-most-decisive
#: first, then the cross-connector functional probe. The op ids are the
#: fixed part of the contract; the caller supplies the params (sentinel
#: path, first-boot log path, unit list, kernel-parameter keys).
_DAY0_LINUX_STEPS: tuple[str, ...] = (
    "linux.file.read",  # 1. completion sentinel -- did first-boot finish?
    "linux.log.tail",  # 2. first-boot log -- why did it abort?
    "linux.service.status",  # 3. each declared unit -- is the subsystem up?
    "linux.sysctl.read",  # 4. kernel parameter -- the live value, not the .conf
    "linux.firewall.show",  # 5. default-deny ruleset actually loaded?
    "linux.mount.list",  # 6. base NFS export live?
)

#: Step 7: the functional probe is served by the ``net`` diagnostics
#: connector (``net-probe-1.x``), referenced not duplicated. These are net
#: verbs -- never linux verbs.
_DAY0_CROSS_CONNECTOR_STEPS: tuple[str, ...] = (
    "net.dns_lookup",
    "net.ntp_check",
)

_BY_ID: dict[str, object] = {op.op_id: op for op in LINUX_OPS}


@pytest.mark.parametrize("op_id", _DAY0_LINUX_STEPS)
def test_recipe_linux_step_is_a_shipped_safe_read_op(op_id: str) -> None:
    """Each Linux recipe step names a real, shipped, safe, read-only op."""
    assert op_id in _BY_ID, f"{op_id!r} recipe step is not a shipped linux op"
    op = _BY_ID[op_id]
    assert op.safety_level == "safe", f"{op_id!r} is not safe-tier"
    assert op.requires_approval is False, f"{op_id!r} requires approval"
    assert "read-only" in op.tags, f"{op_id!r} is not read-only"


@pytest.mark.parametrize("op_id", _DAY0_LINUX_STEPS)
def test_recipe_linux_step_resolves_to_a_curated_group(op_id: str) -> None:
    """Each Linux recipe step's group has a curated when-to-use blurb."""
    op = _BY_ID[op_id]
    assert op.group_key is not None, f"{op_id!r} declares no group"
    assert op.group_key in LINUX_WHEN_TO_USE_BY_GROUP, (
        f"{op_id!r} group {op.group_key!r} has no curated when-to-use blurb"
    )


@pytest.mark.parametrize("op_id", _DAY0_CROSS_CONNECTOR_STEPS)
def test_recipe_functional_probe_is_a_net_verb_not_a_linux_verb(op_id: str) -> None:
    """Step 7 references the net connector, not a (nonexistent) linux verb."""
    assert op_id.startswith("net."), f"{op_id!r} is not a net verb"
    assert not op_id.startswith("linux."), f"{op_id!r} must not be a linux verb"
    assert op_id not in _BY_ID, (
        f"{op_id!r} must be served by the net connector, not a registered linux op"
    )


async def test_recipe_functional_probe_ops_exist_safe_and_probe_grouped() -> None:
    """net.dns_lookup / net.ntp_check are shipped, safe, probe-group net ops.

    Capture what the net registrars pass to ``register_typed_operation``
    without touching the DB: patch the symbol in each registrar's module,
    run the registrars, and read the recorded op metadata.
    """
    from meho_backplane.connectors.net import ntp as net_ntp
    from meho_backplane.connectors.net import ops as net_ops

    captured: dict[str, dict] = {}

    async def _capture(**kwargs: object) -> None:
        captured[str(kwargs["op_id"])] = dict(kwargs)

    with (
        patch.object(net_ops, "register_typed_operation", AsyncMock(side_effect=_capture)),
        patch.object(net_ntp, "register_typed_operation", AsyncMock(side_effect=_capture)),
    ):
        await net_ops.register_net_typed_operations(embedding_service=None)
        await net_ntp.register_net_ntp_check_operation(embedding_service=None)

    for op_id in _DAY0_CROSS_CONNECTOR_STEPS:
        assert op_id in captured, f"{op_id!r} is not registered by the net connector"
        meta = captured[op_id]
        assert meta["safety_level"] == "safe", f"{op_id!r} is not safe-tier"
        assert meta["requires_approval"] is False, f"{op_id!r} requires approval"
        assert meta["group_key"] == "probe", f"{op_id!r} is not in the probe group"


def _section(text: str, start_marker: str, end_marker: str | None = None) -> str:
    assert start_marker in text, f"missing section marker {start_marker!r}"
    body = text[text.index(start_marker) :]
    if end_marker is not None and end_marker in body:
        body = body[: body.index(end_marker)]
    return body


def test_recipe_is_documented_in_connectors_linux_doc() -> None:
    """The connectors-linux.md day-0 recipe section cites every recipe op."""
    section = _section(_LINUX_DOC.read_text(encoding="utf-8"), "## Day-0 verification recipe")
    for op_id in (*_DAY0_LINUX_STEPS, *_DAY0_CROSS_CONNECTOR_STEPS):
        assert op_id in section, f"{op_id!r} is not documented in the day-0 recipe section"


def test_recipe_is_wired_into_consumer_onboarding_template() -> None:
    """The consumer-onboarding template cites every recipe op in its gate."""
    section = _section(
        _CONSUMER_TEMPLATE.read_text(encoding="utf-8"),
        "### Post-configure verification gate",
        "### Audit (canonical history)",
    )
    for op_id in (*_DAY0_LINUX_STEPS, *_DAY0_CROSS_CONNECTOR_STEPS):
        assert op_id in section, f"{op_id!r} is not wired into the consumer template gate"
