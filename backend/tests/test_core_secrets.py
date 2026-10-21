# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The built-in core's ``secrets`` Job (#4041): make missing Secrets, keep the rest.

The Job runs against a fake Secret store here. The chart side (hooks, rights,
the rendered plan) is covered by ``test_chart_core_secrets.py``. Tests check
the length, alphabet and format of made values, never a fixed value.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.core import secrets as core_secrets
from meho_backplane.core.secrets import (
    MANAGED_BY_LABELS,
    PLAN_ENV,
    CoreSecretsError,
    KubernetesSecretStore,
    Plan,
    load_plan,
    reconcile,
)
from meho_backplane.core.tls import VALIDITY_DAYS, self_signed_tls

_ALNUM_32 = re.compile(r"^[A-Za-z0-9]{32}$")

_FULL_PLAN: dict[str, Any] = {
    "make": [
        {"name": "rel-core-postgres-superuser", "kind": "postgres-superuser"},
        {"name": "rel-core-postgres-meho", "kind": "postgres-meho", "host": "rel-postgres"},
        {"name": "rel-core-postgres-keycloak", "kind": "postgres-keycloak"},
        {"name": "rel-core-keycloak-bootstrap", "kind": "keycloak-bootstrap"},
        {"name": "rel-core-keycloak-admin", "kind": "keycloak-admin"},
        {"name": "rel-core-ui-auth", "kind": "ui-auth"},
        {"name": "rel-core-check-runner", "kind": "check-runner"},
        {"name": "rel-core-approval-handoff", "kind": "approval-handoff"},
        {"name": "rel-core-broadcast-auth", "kind": "broadcast-auth"},
        {"name": "rel-core-tls", "kind": "tls", "host": "meho.example.org"},
    ],
    "check": [],
}

_EXPECTED_KEYS = {
    "rel-core-postgres-superuser": {"password"},
    "rel-core-postgres-meho": {"password", "url"},
    "rel-core-postgres-keycloak": {"password"},
    "rel-core-keycloak-bootstrap": {"client_id", "client_secret"},
    "rel-core-keycloak-admin": {"client_secret"},
    "rel-core-ui-auth": {"session_encryption_key", "client_secret"},
    "rel-core-check-runner": {"client_secret"},
    "rel-core-approval-handoff": {"approval_handoff_encryption_key"},
    "rel-core-broadcast-auth": {"requirepass"},
    "rel-core-tls": {"tls.crt", "tls.key", "ca.crt"},
}


class FakeStore:
    """In-memory :class:`~meho_backplane.core.secrets.SecretStore`."""

    def __init__(
        self,
        existing: Mapping[str, Mapping[str, str]] | None = None,
        read_errors: Mapping[str, str] | None = None,
        create_conflicts: frozenset[str] = frozenset(),
    ) -> None:
        self.secrets: dict[str, dict[str, str]] = {k: dict(v) for k, v in (existing or {}).items()}
        self.types: dict[str, str] = {}
        self.created: list[str] = []
        self.reads: list[str] = []
        self._read_errors = dict(read_errors or {})
        self._create_conflicts = create_conflicts

    async def read_keys(self, name: str) -> frozenset[str] | None:
        self.reads.append(name)
        if name in self._read_errors:
            raise CoreSecretsError(f'cannot read Secret "{name}": {self._read_errors[name]}')
        secret = self.secrets.get(name)
        return None if secret is None else frozenset(secret)

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool:
        if name in self._create_conflicts:
            return False
        self.secrets[name] = dict(data)
        self.types[name] = secret_type
        self.created.append(name)
        return True


def _plan(**overrides: Any) -> Plan:
    return Plan.model_validate({**_FULL_PLAN, **overrides})


def _all_values(store: FakeStore) -> list[str]:
    return [value for name in store.created for value in store.secrets[name].values()]


async def test_fresh_install_makes_every_secret_with_its_keys(
    capfd: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore()
    await reconcile(_plan(), store)

    assert store.created == [entry["name"] for entry in _FULL_PLAN["make"]]
    for name, keys in _EXPECTED_KEYS.items():
        assert set(store.secrets[name]) == keys, name
    assert store.types["rel-core-tls"] == "kubernetes.io/tls"
    assert {store.types[name] for name in store.created if name != "rel-core-tls"} == {"Opaque"}
    out = capfd.readouterr().out
    for name in store.created:
        assert f"secret {name}: created" in out


async def test_passwords_are_32_alphanumeric_characters() -> None:
    store = FakeStore()
    await reconcile(_plan(), store)

    passwords = [
        store.secrets["rel-core-postgres-superuser"]["password"],
        store.secrets["rel-core-postgres-meho"]["password"],
        store.secrets["rel-core-postgres-keycloak"]["password"],
        store.secrets["rel-core-keycloak-bootstrap"]["client_id"],
        store.secrets["rel-core-keycloak-bootstrap"]["client_secret"],
        store.secrets["rel-core-keycloak-admin"]["client_secret"],
        store.secrets["rel-core-ui-auth"]["client_secret"],
        store.secrets["rel-core-check-runner"]["client_secret"],
        store.secrets["rel-core-broadcast-auth"]["requirepass"],
    ]
    for password in passwords:
        assert _ALNUM_32.match(password)
        assert set(password) <= set(string.ascii_letters + string.digits)
    assert len(set(passwords)) == len(passwords), "every password is made on its own"


async def test_fernet_keys_and_database_url_have_the_right_format() -> None:
    store = FakeStore()
    await reconcile(_plan(), store)

    for name, key in (
        ("rel-core-ui-auth", "session_encryption_key"),
        ("rel-core-approval-handoff", "approval_handoff_encryption_key"),
    ):
        fernet = Fernet(store.secrets[name][key].encode("ascii"))
        assert fernet.decrypt(fernet.encrypt(b"probe")) == b"probe"

    meho = store.secrets["rel-core-postgres-meho"]
    url = urlsplit(meho["url"])
    assert url.scheme == "postgresql+asyncpg"
    assert (url.username, url.password) == ("meho", meho["password"])
    assert (url.hostname, url.port, url.path) == ("rel-postgres", 5432, "/meho")


async def test_existing_core_secret_is_kept_untouched(
    capfd: pytest.CaptureFixture[str],
) -> None:
    kept = {"approval_handoff_encryption_key": "already-there"}
    store = FakeStore(existing={"rel-core-approval-handoff": kept})
    await reconcile(_plan(), store)

    assert "rel-core-approval-handoff" not in store.created
    assert store.secrets["rel-core-approval-handoff"] == kept
    assert "secret rel-core-approval-handoff: kept" in capfd.readouterr().out


async def test_second_run_creates_nothing_and_keeps_every_value() -> None:
    store = FakeStore()
    await reconcile(_plan(), store)
    first = {name: dict(data) for name, data in store.secrets.items()}

    store.created.clear()
    await reconcile(_plan(), store)

    assert store.created == []
    assert store.secrets == first


async def test_operator_named_secret_is_used_untouched(
    capfd: pytest.CaptureFixture[str],
) -> None:
    mine = {"url": "u", "password": "p"}
    plan = _plan(
        make=[e for e in _FULL_PLAN["make"] if e["kind"] != "postgres-meho"],
        check=[
            {
                "name": "my-db",
                "values_key": "postgres.credentialsSecret",
                "keys": ["password", "url"],
            }
        ],
    )
    store = FakeStore(existing={"my-db": mine})
    await reconcile(plan, store)

    assert "my-db" not in store.created
    assert store.secrets["my-db"] == mine
    assert "rel-core-postgres-meho" not in store.secrets
    assert "secret my-db: checked (named in postgres.credentialsSecret)" in capfd.readouterr().out


async def test_operator_named_secret_missing_a_key_fails_and_creates_nothing() -> None:
    plan = _plan(
        check=[{"name": "my-ui", "values_key": "uiConsole.secretName", "keys": ["client_secret"]}]
    )
    store = FakeStore(existing={"my-ui": {"session_encryption_key": "k"}})

    with pytest.raises(CoreSecretsError) as caught:
        await reconcile(plan, store)

    message = str(caught.value)
    assert '"my-ui"' in message
    assert '"client_secret"' in message
    assert "uiConsole.secretName" in message
    assert store.created == []


async def test_operator_named_secret_that_does_not_exist_fails() -> None:
    plan = _plan(
        check=[{"name": "gone", "values_key": "approvalHandoff.secretName", "keys": ["k"]}]
    )
    store = FakeStore()

    with pytest.raises(CoreSecretsError, match=r'"gone".*does not exist'):
        await reconcile(plan, store)
    assert store.created == []


async def test_kept_core_secret_missing_a_key_fails() -> None:
    store = FakeStore(existing={"rel-core-ui-auth": {"client_secret": "x"}})

    with pytest.raises(CoreSecretsError) as caught:
        await reconcile(_plan(), store)

    assert '"rel-core-ui-auth"' in str(caught.value)
    assert '"session_encryption_key"' in str(caught.value)
    assert store.created == []


@pytest.mark.parametrize("failing", ["rel-core-postgres-superuser", "rel-core-tls", "my-db"])
async def test_read_error_stops_the_job_before_anything_is_created(failing: str) -> None:
    plan = _plan(
        check=[{"name": "my-db", "values_key": "postgres.credentialsSecret", "keys": ["url"]}]
    )
    store = FakeStore(existing={"my-db": {"url": "u"}}, read_errors={failing: "HTTP 403 Forbidden"})

    with pytest.raises(CoreSecretsError, match=f'cannot read Secret "{failing}"'):
        await reconcile(plan, store)
    assert store.created == []


async def test_create_conflict_means_kept(capfd: pytest.CaptureFixture[str]) -> None:
    store = FakeStore(create_conflicts=frozenset({"rel-core-broadcast-auth"}))
    await reconcile(_plan(), store)

    assert "rel-core-broadcast-auth" not in store.created
    assert "secret rel-core-broadcast-auth: kept" in capfd.readouterr().out


async def test_output_never_contains_a_secret_value(capfd: pytest.CaptureFixture[str]) -> None:
    store = FakeStore(existing={"my-ui": {"client_secret": "x"}})
    plan = _plan(
        check=[{"name": "my-ui", "values_key": "uiConsole.secretName", "keys": ["client_secret"]}]
    )
    await reconcile(plan, store)
    await reconcile(plan, store)

    captured = capfd.readouterr()
    output = captured.out + captured.err
    values = _all_values(store)
    assert len(values) == sum(len(keys) for keys in _EXPECTED_KEYS.values())
    for value in values:
        for line in value.splitlines():
            if line and not line.startswith("-----"):
                assert line not in output


def test_main_reports_one_line_and_no_value_on_failure(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore(existing={"my-ui": {"session_encryption_key": "k"}})
    plan = _plan(
        check=[{"name": "my-ui", "values_key": "uiConsole.secretName", "keys": ["client_secret"]}]
    )

    async def fake_run(plan: Plan, namespace: str) -> None:
        assert namespace == "meho"
        await reconcile(plan, store)

    monkeypatch.setattr(core_secrets, "_run_in_cluster", fake_run)
    monkeypatch.setenv(PLAN_ENV, plan.model_dump_json())
    monkeypatch.setenv("POD_NAMESPACE", "meho")

    assert core_secrets.main() == 1
    err = capfd.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith("core_secrets_failed: ")
    assert '"my-ui"' in err[0]
    assert '"client_secret"' in err[0]


def test_main_succeeds_and_prints_names_only(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()

    async def fake_run(plan: Plan, namespace: str) -> None:
        await reconcile(plan, store)

    monkeypatch.setattr(core_secrets, "_run_in_cluster", fake_run)
    monkeypatch.setenv(PLAN_ENV, json.dumps(_FULL_PLAN))
    monkeypatch.setenv("POD_NAMESPACE", "meho")

    assert core_secrets.main() == 0
    captured = capfd.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == len(_FULL_PLAN["make"])


def test_main_needs_the_namespace(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(PLAN_ENV, json.dumps(_FULL_PLAN))
    monkeypatch.delenv("POD_NAMESPACE", raising=False)

    assert core_secrets.main() == 1
    assert "POD_NAMESPACE is not set" in capfd.readouterr().err


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", "is not set"),
        ("not json", "is not a valid plan"),
        ('{"make": [{"name": "x", "kind": "nope"}]}', "make.0.kind"),
        ('{"make": [{"name": "x", "kind": "tls"}]}', "needs a host"),
        ('{"make": [], "extra": 1}', "extra"),
        ('{"check": [{"name": "x", "values_key": "k", "keys": []}]}', "check.0.keys"),
    ],
)
def test_load_plan_rejects_a_bad_plan(raw: str, problem: str) -> None:
    with pytest.raises(CoreSecretsError, match=re.escape(problem)):
        load_plan({PLAN_ENV: raw} if raw else {})


def test_self_signed_tls_is_a_server_certificate_for_the_host() -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)
    pem = self_signed_tls("meho.example.org", now=now)

    leaf = x509.load_pem_x509_certificate(pem["tls.crt"].encode())
    ca = x509.load_pem_x509_certificate(pem["ca.crt"].encode())
    leaf.verify_directly_issued_by(ca)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["meho.example.org"]
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == timedelta(days=VALIDITY_DAYS)
    assert VALIDITY_DAYS <= 825
    assert leaf.not_valid_before_utc <= now

    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(pem["tls.key"].encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()  # type: ignore[union-attr]


def test_self_signed_tls_long_host_lives_in_the_san_only() -> None:
    host = ("a" * 60) + ".example.org"
    leaf = x509.load_pem_x509_certificate(self_signed_tls(host)["tls.crt"].encode())

    assert leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME) == []
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.critical
    assert san.value.get_values_for_type(x509.DNSName) == [host]


class _FakeResponse:
    """The parts of a kubernetes_asyncio response that ``ApiException`` reads."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Reason"
        self.data = json.dumps({"kind": "Status", "message": f"status {status} message"}).encode()

    def getheaders(self) -> dict[str, str]:
        return {"X-Trace": "header-must-not-leak"}


class _FakeCoreV1:
    def __init__(self, read_status: int | None = None, create_status: int | None = None) -> None:
        self.read_status = read_status
        self.create_status = create_status
        self.bodies: list[Any] = []

    @staticmethod
    def _error(status: int) -> ApiException:
        return ApiException(http_resp=_FakeResponse(status))

    async def read_namespaced_secret(self, name: str, namespace: str, **kwargs: Any) -> Any:
        assert kwargs == {"_request_timeout": 30}
        if self.read_status is not None:
            raise self._error(self.read_status)
        return SimpleNamespace(data={"a": "YQ==", "b": "Yg=="})

    async def create_namespaced_secret(self, namespace: str, body: Any, **kwargs: Any) -> Any:
        assert kwargs == {"_request_timeout": 30}
        self.bodies.append(body)
        if self.create_status is not None:
            raise self._error(self.create_status)
        return body


async def test_kubernetes_store_reads_key_names_and_maps_not_found() -> None:
    assert await KubernetesSecretStore(_FakeCoreV1(), "ns").read_keys("x") == frozenset({"a", "b"})
    assert await KubernetesSecretStore(_FakeCoreV1(read_status=404), "ns").read_keys("x") is None


async def test_kubernetes_store_read_error_names_the_status_not_the_headers() -> None:
    store = KubernetesSecretStore(_FakeCoreV1(read_status=403), "ns")

    with pytest.raises(CoreSecretsError) as caught:
        await store.read_keys("x")

    message = str(caught.value)
    assert message == 'cannot read Secret "x": HTTP 403 Reason: status 403 message'
    assert "header-must-not-leak" not in message


async def test_kubernetes_store_creates_with_the_managed_by_label_only() -> None:
    api = _FakeCoreV1()
    store = KubernetesSecretStore(api, "ns")

    assert await store.create("rel-core-tls", {"tls.crt": "c"}, "kubernetes.io/tls") is True
    body = api.bodies[0]
    assert body.metadata.name == "rel-core-tls"
    assert body.metadata.labels == dict(MANAGED_BY_LABELS)
    assert body.metadata.annotations is None
    assert body.type == "kubernetes.io/tls"
    assert body.string_data == {"tls.crt": "c"}


async def test_kubernetes_store_create_conflict_and_error() -> None:
    assert (
        await KubernetesSecretStore(_FakeCoreV1(create_status=409), "ns").create(
            "x", {"k": "v"}, "Opaque"
        )
        is False
    )
    with pytest.raises(CoreSecretsError, match=r'cannot create Secret "x": HTTP 422 Reason'):
        await KubernetesSecretStore(_FakeCoreV1(create_status=422), "ns").create(
            "x", {"k": "v"}, "Opaque"
        )
