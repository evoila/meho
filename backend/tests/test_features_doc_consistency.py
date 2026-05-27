# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI gate: ``/ready.features.<gate>.missing_env`` matches the upstream doc.

G0.15-T5 (#1214) is the regression-prevention gate for the v0.7.0
``ui_surface.missing_env`` miss (``UI_SESSION_ENCRYPTION_KEY`` documented
under ``docs/cross-repo/keycloak-web-client.md`` Check 3 but not listed
by :func:`meho_backplane.features.build_features_block`). Three releases
in a row a documented env var slipped past the ``/ready`` self-doc and
the operator only discovered it by hitting the surface and failing —
defeating the whole point of the "documentation as data" shape G0.14-T7
(#1148) introduced.

The gate is parameterised over every feature gate that carries a
``docs`` reference. For each gate it asserts two things:

1. **Doc presence** — every env var the test contract names appears in
   the gate's upstream doc. Catches the inverse drift: the doc dropped
   a var that's still gated in code. The doc is the operator-facing
   surface; the code follows the doc, not the other way around.
2. **`missing_env` parity** — with the corresponding settings unset,
   ``build_features_block(settings).<gate>.missing_env`` exactly
   equals the contract set. Order is checked too: the
   ``features.py`` block emits env vars in a stable declaration
   order operator tooling can pin against.

The test contract is the **canonical** list per gate — a `set` here,
not "whatever the doc says today". The contract lives here so a
single one-line change advertises the new env var to operators (and
the failing test reminds the implementer to update
``features.py`` + the doc together). Adding a new env var to a gate
is a three-touch change:

* :mod:`meho_backplane.features` — the gate block lists the new var.
* :mod:`meho_backplane.settings` — the field + ``from_env`` reader.
* This test contract — the new var name.

Plus the doc must mention it (otherwise the doc-presence check
fails). That's the gate: a missing one of the four touches breaks
the test.

Gates without a ``docs`` reference (``audit_replay``,
``approval_queue``) are out of scope here — they don't have a doc
contract to validate against. The transitive ``approval_queue``
gates ``configured`` on ``agent_runtime``, not on its own env vars,
and ``audit_replay`` is feature-coupled to MCP itself (no
operator-configurable knobs). Both shapes are pinned in
:mod:`test_features` directly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.features import build_features_block
from meho_backplane.settings import Settings

# ---------------------------------------------------------------------------
# Contract: documented env vars per feature gate
# ---------------------------------------------------------------------------
#
# Each entry maps to:
#   * ``docs`` — path (relative to the repo root) the gate's ``docs``
#     field cites.
#   * ``env_vars`` — the env vars the doc requires for the feature to
#     be operative. Order = the declaration order the gate's
#     ``missing_env`` block emits.
#   * ``settings_attrs`` — the corresponding :class:`Settings` field
#     names. Parallel to ``env_vars`` index-by-index. Required so the
#     test can build a synthetic Settings with exactly those fields
#     unset and the rest fully wired (proves that *only* these fields
#     drive the gate's ``missing_env``).
#
# Audit history when this contract was last refreshed: G0.15-T5
# (#1214); see the issue body for the table of doc-vs-block drift
# across all four gates.
FEATURE_GATE_CONTRACT: dict[str, dict[str, Any]] = {
    "agent_runtime": {
        "docs": "docs/cross-repo/keycloak-agent-client.md",
        "env_vars": (
            "KEYCLOAK_ADMIN_URL",
            "KEYCLOAK_ADMIN_CLIENT_ID",
            "KEYCLOAK_ADMIN_CLIENT_SECRET",
        ),
        "settings_attrs": (
            "keycloak_admin_url",
            "keycloak_admin_client_id",
            "keycloak_admin_client_secret",
        ),
    },
    "ui_surface": {
        "docs": "docs/cross-repo/keycloak-web-client.md",
        "env_vars": (
            "UI_KEYCLOAK_CLIENT_ID",
            "UI_KEYCLOAK_CLIENT_SECRET",
            "UI_SESSION_ENCRYPTION_KEY",
        ),
        "settings_attrs": (
            "ui_keycloak_client_id",
            "ui_keycloak_client_secret",
            "ui_session_encryption_key",
        ),
    },
}


def _repo_root(start: Path) -> Path:
    """Walk up from the test file to the repo root (where docs/ lives)."""
    here = start.resolve()
    for parent in (here, *here.parents):
        if (parent / "docs" / "cross-repo").is_dir():
            return parent
    raise RuntimeError(
        "could not find repo root containing docs/cross-repo/ "
        "(expected to be reachable from the backend/tests/ test file)"
    )


REPO_ROOT = _repo_root(Path(__file__))

# Matches ALL_CAPS_WITH_UNDERSCORES tokens of length >= 3. The env vars
# the gates expose follow this shape uniformly (KEYCLOAK_ADMIN_URL,
# UI_SESSION_ENCRYPTION_KEY, etc.).
_ENV_VAR_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def _settings_fully_wired(**unset_overrides: str) -> Settings:
    """Build a :class:`Settings` with every gate-relevant field wired.

    Tests then override the specific fields they want unset. Non-gate
    fields are pinned to valid sentinels so construction cannot fail
    on an unrelated validator.

    The ``ui_session_encryption_key`` default is a low-entropy
    placeholder — Fernet's actual key validator only runs at
    session-store init time, not at Settings construction, so any
    non-empty string is acceptable here. Empty-string is "unset"
    for the gate's purposes per the convention
    ``not settings.<attr>``.
    """
    base: dict[str, Any] = {
        "keycloak_issuer_url": "https://keycloak.test/realms/meho",
        "keycloak_audience": "meho-backplane",
        "vault_addr": "https://vault.test",
        "database_url": "postgresql+asyncpg://meho:secret@db.test:5432/meho",
        "keycloak_admin_url": "https://keycloak.test/admin/realms/meho",
        "keycloak_admin_client_id": "meho-admin",
        "keycloak_admin_client_secret": "s3cret",
        "ui_keycloak_client_id": "meho-web",
        "ui_keycloak_client_secret": "s3cret",
        "ui_session_encryption_key": "non-empty-placeholder",
        "mcp_require_session_id": False,
    }
    base.update(unset_overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("gate_name", list(FEATURE_GATE_CONTRACT.keys()))
def test_gate_doc_mentions_every_contracted_env_var(gate_name: str) -> None:
    """Every contracted env var appears verbatim in the gate's upstream doc.

    This is the doc-side check: if a gate's contract names an env
    var the doc has dropped or renamed, the operator's
    documentation-as-data trail is broken. The fix is either
    (a) restore the var to the doc, or (b) remove it from the
    contract + the ``features.py`` block + ``settings.py`` — but
    the *test must not pass* until the three sides agree.
    """
    contract = FEATURE_GATE_CONTRACT[gate_name]
    doc_path = REPO_ROOT / contract["docs"]
    assert doc_path.is_file(), (
        f"gate {gate_name!r} cites {contract['docs']!r} but the file "
        f"does not exist at {doc_path}. Either the path is wrong in "
        f"features.py / this contract, or the doc was removed."
    )

    doc_text = doc_path.read_text(encoding="utf-8")
    tokens_in_doc = set(_ENV_VAR_TOKEN_RE.findall(doc_text))

    missing_from_doc = [env_var for env_var in contract["env_vars"] if env_var not in tokens_in_doc]
    assert not missing_from_doc, (
        f"gate {gate_name!r} contracts on env var(s) {missing_from_doc!r} "
        f"but {contract['docs']!r} does not mention them. Either add them "
        f"to the doc (the operator-facing source of truth) or remove them "
        f"from the contract."
    )


@pytest.mark.parametrize("gate_name", list(FEATURE_GATE_CONTRACT.keys()))
def test_gate_missing_env_lists_every_contracted_var_when_unset(gate_name: str) -> None:
    """With every contracted env var unset, ``missing_env`` matches the contract.

    This is the code-side check: ``features.py`` must enumerate the
    same env vars the doc documents, in the same order. The order
    pin is load-bearing — operator tooling that builds a remediation
    chain from ``missing_env`` reads it positionally; reshuffling
    the list is a wire-compat break.
    """
    contract = FEATURE_GATE_CONTRACT[gate_name]
    unset_overrides = dict.fromkeys(contract["settings_attrs"], "")
    block = build_features_block(_settings_fully_wired(**unset_overrides))

    gate = block[gate_name]
    assert gate["configured"] is False, (
        f"gate {gate_name!r} should be unconfigured when its env vars "
        f"are unset; got configured=True. The gate's block likely "
        f"checks a different settings field than the contract declares."
    )
    assert tuple(gate["missing_env"]) == contract["env_vars"], (
        f"gate {gate_name!r} missing_env drifted from the doc/contract.\n"
        f"  expected (declaration order): {contract['env_vars']!r}\n"
        f"  actual: {tuple(gate['missing_env'])!r}\n"
        f"Either update features.py to match the doc, or update the "
        f"contract + doc together."
    )


@pytest.mark.parametrize("gate_name", list(FEATURE_GATE_CONTRACT.keys()))
def test_gate_fully_wired_yields_empty_missing_env(gate_name: str) -> None:
    """Every contracted env var set → ``configured: true`` + empty list.

    Catches the inverse: a code-side check that consults a settings
    field outside the contract (e.g. a stray dependency on a chassis
    setting) would leave ``missing_env`` non-empty even when every
    contracted var is wired. The test fails in that case because
    the helper's defaults wire the chassis vars too.
    """
    block = build_features_block(_settings_fully_wired())
    gate = block[gate_name]
    assert gate["configured"] is True, (
        f"gate {gate_name!r} should be configured when every contracted "
        f"env var is set; got configured=False with "
        f"missing_env={gate.get('missing_env')!r}. The gate likely depends "
        f"on a setting outside the contract."
    )
    assert gate["missing_env"] == [], (
        f"gate {gate_name!r} should have empty missing_env when fully "
        f"wired; got {gate['missing_env']!r}."
    )


@pytest.mark.parametrize("gate_name", list(FEATURE_GATE_CONTRACT.keys()))
def test_gate_docs_field_matches_contract(gate_name: str) -> None:
    """The block's ``docs`` field equals the contract's doc path.

    Pins the operator-facing doc reference so a rename of the file
    in ``docs/cross-repo/`` doesn't silently rot the ``/ready``
    payload's pointer. Same shape as the test in :mod:`test_features`
    for individual gates, parameterised here over the doc-bearing set.
    """
    contract = FEATURE_GATE_CONTRACT[gate_name]
    block = build_features_block(_settings_fully_wired())
    gate = block[gate_name]
    assert gate["docs"] == contract["docs"], (
        f"gate {gate_name!r} carries docs={gate['docs']!r}; contract "
        f"expects {contract['docs']!r}. If the doc moved, update the "
        f"contract and features.py together."
    )
