# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the product form of a connector with no version can only add.

Two products, ``cowx`` and ``cowy``, each have operations with no
implementation id and no version. With ``CUSTOM_POLICY`` on, the policy
names such a connector by its product. Each operation is classified twice
in each scope: once by the form with the dash (``-``) and once by the
product. The result is the stricter of the two: a wait of either form, the
higher risk level, a conflict of either form; the category and the labels
of the form with the dash. So an entry that matches only by the product can
add a wait or raise a level, and never takes a wait away, lowers a level or
changes the category or the labels. Each case runs through
``POST /api/v1/operations/call``, ``POST /api/v1/policy/explain`` and
``GET /api/v1/policy/carry-over/dry-run``. All names and values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.broadcast import events
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, ApprovalRequest, EndpointDescriptor, Tenant
from meho_backplane.main import app
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.store import classify_rows
from meho_backplane.settings import get_settings

from ._carry_over_helpers import TENANT, auth, dry_run, policy, token
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

PRODUCTS = ("cowx", "cowy")
RUN = "wild.thing.run"
SECRET = "wild.secret.read"
PROBE = "wild.target.probe"
DROP = "wild.thing.drop"
WAIT = "awaiting_approval"

#: Each caller of the calls: (name, token claims).
CALLERS: list[tuple[str, dict[str, Any]]] = [
    ("person, operator", {"sub": "op-a", "tenant_role": TenantRole.OPERATOR.value}),
    ("person, tenant admin", {"sub": "admin-a"}),
    ("service login", {"sub": "svc-a", "principal_kind": "service"}),
    (
        "agent login with a grant for every operation",
        {"sub": "agent-g", "principal_kind": "agent", "tenant_role": TenantRole.OPERATOR.value},
    ),
]
OPERATOR = CALLERS[0][1]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


@pytest.fixture(autouse=True)
def switches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.setattr(events, "_CREDENTIAL_READ_OPS", events._CREDENTIAL_READ_OPS | {SECRET})
    get_settings.cache_clear()


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"ran": True}


_SCHEMAS: dict[str, dict[str, Any]] = {
    PROBE: {
        "type": "object",
        "properties": {"host": {"type": "string", CALLER_ADDRESS_MARKER: True}},
    },
}
_LEVELS = {RUN: "safe", SECRET: "caution", PROBE: "caution", DROP: "dangerous"}


async def _register(embedding: AsyncMock, op_id: str) -> None:
    for product in PRODUCTS:
        await register_typed_operation(
            product=product,
            version="",
            impl_id="",
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema=_SCHEMAS.get(op_id, {"type": "object", "properties": {}}),
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level=_LEVELS[op_id],  # type: ignore[arg-type]
            embedding_service=embedding,
        )


async def _grant_agent() -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=TENANT, slug="tenant-a", name="Tenant A"))
        await session.flush()
        session.add(
            AgentPermission(
                tenant_id=TENANT,
                principal_sub="agent-g",
                op_pattern="*",
                verdict="auto-execute",
                created_by_sub="admin-a",
            )
        )
        await session.commit()


def _call(client: TestClient, key: Any, product: str, op_id: str, claims: dict[str, Any]) -> str:
    params = {"host": "cow.example"} if op_id == PROBE else {}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/operations/call",
            json={"connector_id": product, "op_id": op_id, "params": params},
            headers=auth(token(key, **claims)),
        )
    assert resp.status_code == 200, resp.text
    status: str = resp.json()["status"]
    return status


def _explain(client: TestClient, key: Any, product: str, op_id: str) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/policy/explain",
            json={"connector_id": product, "op_id": op_id, "me": True},
            headers=auth(token(key)),
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()["classification"]
    return body


def _listed(client: TestClient, key: Any, scope: str) -> dict[str, list[str]]:
    """Each "always wait" entry of the dry run of *scope*, with the products it lists."""
    claims: dict[str, Any] = {"platform_admin": True} if scope == "install" else {}
    resp = dry_run(client, key, scope, **claims)
    assert resp.status_code == 200, resp.text
    return {
        setting["ref"]: [op["connector_id"] for op in setting["operations"]]
        for setting in resp.json()["settings"]
    }


async def _classified(product: str, op_id: str) -> Any:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == product, EndpointDescriptor.op_id == op_id
                )
            )
        ).scalar_one()
        (classified,) = await classify_rows(session, tenant_id=TENANT, rows=[row], overrides=[[]])
    return classified


def _scope_id(scope: str) -> uuid.UUID | None:
    return None if scope == "install" else TENANT


# ---------------------------------------------------------------------------
# Cases 1 to 8: no call runs that the form with the dash makes wait
# ---------------------------------------------------------------------------

_DASH_WAITS = [
    {"match": {"connector": "*-*"}, "always_wait": True},
    {"match": {"connector": "cowx"}, "always_wait": False},
]

#: (case, scope, entries, callers, call of cowx, call of cowy, dry run lists)
_WAIT_CASES: list[tuple[str, str, list[dict[str, Any]], list[int], str, str, list[str]]] = [
    ("1-4", "tenant", _DASH_WAITS, [0, 1, 2, 3], WAIT, WAIT, ["cowx", "cowy"]),
    ("5", "install", _DASH_WAITS, [0, 1, 2, 3], WAIT, WAIT, ["cowx", "cowy"]),
    (
        "6",
        "tenant",
        [
            {"match": {"connector": "-"}, "always_wait": True},
            {"match": {"connector": "cowx"}, "always_wait": False},
        ],
        [0],
        WAIT,
        WAIT,
        ["cowx", "cowy"],
    ),
    (
        "7",
        "tenant",
        [
            {"match": {"connector": "cowx"}, "always_wait": True},
            {"match": {"connector": "*-*"}, "always_wait": False},
        ],
        [0],
        WAIT,
        "ok",
        ["cowx"],
    ),
    ("8", "tenant", [], [0], "ok", "ok", []),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "scope", "entries", "callers", "cowx", "cowy", "listed"),
    _WAIT_CASES,
    ids=[f"case-{c[0]}" for c in _WAIT_CASES],
)
async def test_a_wait_of_the_form_with_the_dash_holds_in_both_scopes(
    client: TestClient,
    embedding: AsyncMock,
    case: str,
    scope: str,
    entries: list[dict[str, Any]],
    callers: list[int],
    cowx: str,
    cowy: str,
    listed: list[str],
) -> None:
    await _register(embedding, RUN)
    await _grant_agent()
    if entries:
        await policy(_scope_id(scope), entries)
    key = make_rsa_keypair(f"kid-cpf-wait-{case}")

    for index in callers:
        name, claims = CALLERS[index]
        assert _call(client, key, "cowx", RUN, claims) == cowx, (case, name)
        assert _call(client, key, "cowy", RUN, claims) == cowy, (case, name)
    found = _listed(client, key, scope)

    if listed:
        assert found[f"{scope}:classifications[0]"] == listed
    else:
        assert found == {}


# ---------------------------------------------------------------------------
# Cases 14 to 21: field by field the stricter of the two forms
# ---------------------------------------------------------------------------

#: (case, scope, op, entries, {product: (risk, always_wait, category)}, call, dry run lists)
_FIELD_CASES: list[
    tuple[
        str,
        str,
        str,
        list[dict[str, Any]],
        dict[str, tuple[str, bool, str]],
        dict[str, str],
        dict[str, list[str]],
    ]
] = [
    (
        "14",
        "tenant",
        RUN,
        [
            {"match": {"connector": "*-*"}, "risk": "dangerous"},
            {"match": {"connector": "cow*"}, "risk": "caution"},
        ],
        {"cowx": ("dangerous", False, "other"), "cowy": ("dangerous", False, "other")},
        {},
        {},
    ),
    (
        "15",
        "install",
        RUN,
        [
            {"match": {"connector": "*"}, "always_wait": True},
            {"match": {"connector": "cow*"}, "always_wait": False},
        ],
        {"cowx": ("safe", True, "other"), "cowy": ("safe", True, "other")},
        {"cowx": WAIT, "cowy": WAIT},
        {"install:classifications[0]": ["cowx", "cowy"]},
    ),
    (
        "16",
        "tenant",
        RUN,
        [
            {"match": {"connector": "*-*"}, "category": "storage"},
            {"match": {"connector": "cowx"}, "category": "network"},
        ],
        {"cowx": ("safe", False, "storage")},
        {"cowx": "ok"},
        {},
    ),
    (
        "17",
        "tenant",
        SECRET,
        [
            {"match": {"connector": "*-*"}, "always_wait": True},
            {"match": {"connector": "cow*"}, "always_wait": False},
        ],
        {"cowx": ("caution", True, "other"), "cowy": ("caution", True, "other")},
        {"cowx": WAIT, "cowy": WAIT},
        {"tenant:classifications[0]": ["cowx", "cowy"]},
    ),
    (
        "18",
        "tenant",
        PROBE,
        [
            {"match": {"connector": "*-*"}, "always_wait": True},
            {"match": {"connector": "co?x"}, "always_wait": False},
        ],
        {"cowx": ("caution", True, "other")},
        {"cowx": WAIT},
        {"tenant:classifications[0]": ["cowx", "cowy"]},
    ),
    (
        "19",
        "tenant",
        DROP,
        [{"match": {"connector": "cow*"}, "risk": "safe"}],
        {"cowx": ("dangerous", False, "other")},
        {},
        {},
    ),
    (
        "20",
        "tenant",
        RUN,
        [{"match": {"connector": "cowx"}, "risk": "dangerous"}],
        {"cowx": ("dangerous", False, "other"), "cowy": ("safe", False, "other")},
        {"cowy": "ok"},
        {},
    ),
    (
        "21",
        "install",
        RUN,
        [{"match": {"connector": "cow*"}, "always_wait": True}],
        {"cowx": ("safe", True, "other"), "cowy": ("safe", True, "other")},
        {"cowx": WAIT, "cowy": WAIT},
        {"install:classifications[0]": ["cowx", "cowy"]},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "scope", "op_id", "entries", "classified", "calls", "listed"),
    _FIELD_CASES,
    ids=[f"case-{c[0]}" for c in _FIELD_CASES],
)
async def test_the_product_form_only_adds_a_wait_or_raises_a_level(
    client: TestClient,
    embedding: AsyncMock,
    case: str,
    scope: str,
    op_id: str,
    entries: list[dict[str, Any]],
    classified: dict[str, tuple[str, bool, str]],
    calls: dict[str, str],
    listed: dict[str, list[str]],
) -> None:
    await _register(embedding, op_id)
    await policy(_scope_id(scope), entries)
    key = make_rsa_keypair(f"kid-cpf-field-{case}")

    for product, (risk, always_wait, category) in classified.items():
        answer = _explain(client, key, product, op_id)
        assert answer["risk_level"] == risk, (case, product, answer)
        assert answer["category"] == category, (case, product, answer)
        assert answer["labels"] == {}, (case, product, answer)
        assert answer["requires_approval"] is (always_wait or op_id == PROBE), (case, answer)
        stored = await _classified(product, op_id)
        assert stored.always_wait is always_wait, (case, product, stored)
        assert stored.category == category, (case, product, stored)
    for product, status in calls.items():
        assert _call(client, key, product, op_id, OPERATOR) == status, (case, product)
    assert _listed(client, key, scope) == listed


# ---------------------------------------------------------------------------
# Cases 22 to 25: a tenant entry that weakens an install entry in either form
# ---------------------------------------------------------------------------

_WAIT_REFUSAL = (
    "sets `always_wait: false` on `wild.thing.run`, but `install:classifications[0]` makes it "
    "always wait for the whole install. A tenant can make more operations wait, not fewer."
)
_RISK_REFUSAL = (
    "`classifications[0]` sets `risk: caution`, but `install:classifications[0]` sets "
    "`wild.thing.run` to `dangerous` for the whole install"
)


def _send_file(client: TestClient, key: Any, path: str, entry: dict[str, Any]) -> dict[str, Any]:
    match = ", ".join(f"{k}: {v!r}" for k, v in entry["match"].items())
    rest = ", ".join(f"{k}: {str(v).lower()}" for k, v in entry.items() if k != "match")
    content = f"policy: v1\nclassifications:\n  - {{match: {{{match}}}, {rest}}}\n"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            f"/api/v1/policy/{path}",
            params={"preview": "true"} if path == "import" else None,
            content=content.encode(),
            headers={**auth(token(key)), "Content-Type": "application/yaml"},
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _edit_preview(client: TestClient, key: Any, entry: dict[str, Any]) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/policy/classification/preview", json=entry, headers=auth(token(key))
        )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


#: (case, the install's entry, the tenant's entry)
_FLOOR_WAIT_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "22",
        {"match": {"connector": "-"}, "always_wait": True},
        {"match": {"connector": "cowx"}, "always_wait": False},
    ),
    (
        "23",
        {"match": {"connector": "*-*"}, "always_wait": True},
        {"match": {"connector": "cowx"}, "always_wait": False},
    ),
    (
        "24",
        {"match": {"connector": "cowx"}, "always_wait": True},
        {"match": {"connector": "-"}, "always_wait": False},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "install", "tenant"),
    _FLOOR_WAIT_CASES,
    ids=[f"case-{c[0]}" for c in _FLOOR_WAIT_CASES],
)
async def test_a_tenant_entry_cannot_end_an_install_wait_of_the_other_form(
    client: TestClient,
    embedding: AsyncMock,
    case: str,
    install: dict[str, Any],
    tenant: dict[str, Any],
) -> None:
    await _register(embedding, RUN)
    await policy(None, [install])
    key = make_rsa_keypair(f"kid-cpf-floor-{case}")

    validated = _send_file(client, key, "validate", tenant)
    imported = _send_file(client, key, "import", tenant)
    edit = _edit_preview(client, key, tenant)
    await policy(TENANT, [tenant])
    status = _call(client, key, "cowx", RUN, OPERATOR)

    for answer in (validated, imported):
        assert answer["ok"] is False, (case, answer)
        assert answer["mistakes"] == [
            f"`classifications[0]` {_WAIT_REFUSAL} Remove `always_wait: false` from the tenant "
            "entry; its other fields can stay."
        ], case
    assert edit["conflict"] == (f"This edit {_WAIT_REFUSAL} Do not send `always_wait: false`."), (
        case
    )
    assert status == WAIT, case


@pytest.mark.asyncio
async def test_a_tenant_entry_cannot_lower_an_install_level_of_the_other_form(
    client: TestClient, embedding: AsyncMock
) -> None:
    # Case 25. A single edit sets no risk level, so only the file is checked.
    await _register(embedding, RUN)
    await policy(None, [{"match": {"connector": "*-*"}, "risk": "dangerous"}])
    key = make_rsa_keypair("kid-cpf-floor-25")
    tenant = {"match": {"connector": "cowx"}, "risk": "caution"}

    validated = _send_file(client, key, "validate", tenant)
    imported = _send_file(client, key, "import", tenant)
    await policy(TENANT, [tenant])

    for answer in (validated, imported):
        assert answer["ok"] is False, answer
        (mistake,) = answer["mistakes"]
        assert mistake.startswith(_RISK_REFUSAL), mistake
    assert _explain(client, key, "cowx", RUN)["risk_level"] == "dangerous"
    # A person's call of a `dangerous` operation waits for approval.
    assert _call(client, key, "cowx", RUN, OPERATOR) == WAIT


# ---------------------------------------------------------------------------
# Cases 26 and 27: explain and the approval card name the entry that decided
# ---------------------------------------------------------------------------


def _wait_rule(client: TestClient, key: Any, product: str) -> dict[str, Any]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.post(
            "/api/v1/policy/explain",
            json={"connector_id": product, "op_id": RUN, "me": True},
            headers=auth(token(key)),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "wait", body
    (rule,) = [rule for rule in body["evaluator"]["rules"] if rule["rule_id"] == "always_wait"]
    return dict(rule)


@pytest.mark.asyncio
async def test_case_26_explain_names_the_entry_that_makes_the_call_wait(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _register(embedding, RUN)
    await policy(TENANT, _DASH_WAITS)
    key = make_rsa_keypair("kid-cpf-explain-26")

    assert _wait_rule(client, key, "cowx")["path"] == "tenant:classifications[0]"


@pytest.mark.asyncio
async def test_case_27_the_card_names_the_entry_of_its_category(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _register(embedding, RUN)
    await policy(
        TENANT,
        [
            {"match": {"connector": "*-*"}, "category": "storage"},
            {"match": {"connector": "cowx"}, "always_wait": True},
        ],
    )
    key = make_rsa_keypair("kid-cpf-card-27")

    status = _call(client, key, "cowx", RUN, OPERATOR)
    async with get_sessionmaker()() as session:
        (request,) = (await session.execute(select(ApprovalRequest))).scalars()
    card = request.proposed_effect

    assert status == WAIT
    assert card["category"] == "storage", card
    assert card["classification_rule"] == "tenant:classifications[0]", card
    assert _wait_rule(client, key, "cowx")["path"] == "tenant:classifications[1]"
