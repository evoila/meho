# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The value check of ``k8s.apply`` reads every key and value of a manifest.

Design note. The check walks each key and each value at any depth with one
set of rules. There is no list of parent keys. The rules:

* a key of a ``data``, ``stringData`` or ``binaryData`` map has a secret name;
* any other key has a secret name and a value of its own (text or number);
* an ``env`` entry has a secret-shaped name (a secret word may sit inside a
  longer name) and a value;
* the ``command`` and ``args`` of an object, read as one command line, have
  a value the approval preview hides;
* a field of a text in ``data`` or ``stringData`` names a secret and gives
  it a value (every field of a line);
* a text in ``annotations`` that is JSON is parsed and walked the same way;
* a text has a secret shape.

A manifest that the check flags keeps no copy on the stored request: not
before approval, not after approval, not after the request ends. The stored
approval preview hides a number under a secret name too.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import op_classes
from meho_backplane.connectors.kubernetes.op_classes import (
    MAX_CHECKED_OBJECTS,
    manifest_holds_credential,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    get_request,
    params_with_secret_fields_hidden,
    reject_request,
)
from meho_backplane.settings import get_settings
from tests.test_credential_calls_always_wait import _TENANT, _service
from tests.test_operations_preview_k8s_apply_echo import _preview as _apply_preview

_VALUE = "made-up-value"
_COMMAS = ",,,,,,,"
_ADDRESS = "postgres://app:" + _VALUE + "@db.invalid:5432/app"  # trufflehog:ignore


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _manifest(body: dict[str, Any], kind: str = "Widget") -> str:
    return json.dumps({"apiVersion": "example.invalid/v1", "kind": kind, **body})


def _flagged(manifest: str) -> bool:
    return manifest_holds_credential({"manifest": manifest})


# ---------------------------------------------------------------------------
# The grammar: each place kind times each name rule and shape rule
# ---------------------------------------------------------------------------

_NAMES = ["password", "adminPassword", "api_key", "client-secret", "apiToken"]
_VALUES = {
    "text": _VALUE,
    "number": 123456,
    "shape": _ADDRESS,
}


def _in_place(place: str, key: str, value: Any) -> str:
    metadata: dict[str, Any] = {"name": "w"}
    if place == "top":
        return _manifest({"metadata": metadata, key: value})
    if place == "spec":
        return _manifest({"metadata": metadata, "spec": {"nested": {key: value}}})
    if place == "spec-data":
        return _manifest({"metadata": metadata, "spec": {"data": {key: value}}})
    if place == "annotation":
        return _manifest({"metadata": {**metadata, "annotations": {key: value}}})
    if place == "label":
        return _manifest({"metadata": {**metadata, "labels": {key: value}}})
    if place == "json-annotation":
        text = json.dumps({"spec": {"deep": [{key: value}]}})
        return _manifest({"metadata": {**metadata, "annotations": {"example.invalid/last": text}}})
    raise AssertionError(place)


_PLACES = ["top", "spec", "spec-data", "annotation", "label", "json-annotation"]


@pytest.mark.parametrize("place", _PLACES)
@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("kind", list(_VALUES))
def test_every_place_name_and_shape_is_flagged(place: str, name: str, kind: str) -> None:
    value = _VALUES[kind]
    if kind == "shape":
        # The shape rule works under a plain name too.
        name = "endpoint"
    assert _flagged(_in_place(place, name, value)) is True


def test_the_grammar_table_size() -> None:
    forms = len(_PLACES) * len(_NAMES) * len(_VALUES)
    assert forms == 90


_PLAIN_FORMS = [
    ("mode", "fast"),
    ("password", ""),
    ("password", None),
    ("password", True),
    ("password", {"valueFrom": "x"}),
    ("password", ["a"]),
    ("secretName", "db-login"),
    ("automountServiceAccountToken", False),
]


@pytest.mark.parametrize("place", [p for p in _PLACES if p != "spec-data"])
@pytest.mark.parametrize("key,value", _PLAIN_FORMS)
def test_plain_forms_in_every_place_are_not_flagged(place: str, key: str, value: Any) -> None:
    assert _flagged(_in_place(place, key, value)) is False


def test_a_plain_value_in_a_data_map_is_not_flagged() -> None:
    assert _flagged(_in_place("spec-data", "mode", "fast")) is False


# ---------------------------------------------------------------------------
# env names, command and args, file text
# ---------------------------------------------------------------------------


def _pod(container: dict[str, Any]) -> str:
    return _manifest(
        {
            "metadata": {"name": "d"},
            "spec": {
                "template": {"spec": {"containers": [{"name": "c", "image": "i", **container}]}}
            },
        },
        kind="Deployment",
    )


@pytest.mark.parametrize("name", ["PGPASSWORD", "DB_PASS", "REDIS_AUTH", "AWS_SECRET_ACCESS_KEY"])
def test_an_env_name_with_a_secret_word_inside_counts(name: str) -> None:
    assert _flagged(_pod({"env": [{"name": name, "value": _VALUE}]})) is True


@pytest.mark.parametrize(
    "env",
    [
        [{"name": "LOG_LEVEL", "value": "debug"}],
        [{"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "pw"}}}],
        [{"name": "PGPASSWORD", "value": ""}],
        [{"name": "PGPASSWORD"}],
        ["PGPASSWORD", 5, None, {"name": 7, "value": 1}],
    ],
    ids=["plain", "from-a-reference", "empty-value", "no-value", "wrong-types"],
)
def test_an_env_entry_without_a_secret_value_does_not_count(env: list[Any]) -> None:
    assert _flagged(_pod({"env": env})) is False


@pytest.mark.parametrize(
    "field,items",
    [
        ("args", ["--password=" + _VALUE]),
        ("args", ["--token", _VALUE]),
        ("args", ["PGPASSWORD=" + _VALUE]),
        ("command", ["sh", "-c", "PGPASSWORD=" + _VALUE + " psql"]),
        ("command", ["curl", "-u", "admin:" + _VALUE]),
    ],
)
def test_a_command_item_with_a_secret_counts(field: str, items: list[str]) -> None:
    assert _flagged(_pod({field: items})) is True


def test_a_secret_flag_that_ends_command_and_its_value_in_args_count() -> None:
    assert _flagged(_pod({"command": ["mysql", "--password"], "args": [_VALUE]})) is True


@pytest.mark.parametrize(
    "command",
    [
        {"command": ["curl", "-u"], "args": ["alice"]},
        {"command": ["curl", "-u", "alice"]},
        {"command": ["curl"], "args": ["--user", "alice", "https://x.invalid"]},
    ],
    ids=["user-flag-ends-command", "user-flag-in-command", "user-flag-in-args"],
)
def test_a_user_name_alone_after_a_user_flag_does_not_count(command: dict[str, Any]) -> None:
    assert _flagged(_pod(command)) is False


@pytest.mark.parametrize(
    "command",
    [
        {"command": ["sh", "-c", "echo hello"], "args": ["--verbose", "--port", "8080"]},
        {"command": "run", "args": ["-p", "-x"]},
        {"command": [1, None, {"a": "b"}], "args": {"a": 1}},
    ],
    ids=["plain", "string-command", "wrong-types"],
)
def test_a_plain_command_does_not_count(command: dict[str, Any]) -> None:
    assert _flagged(_pod(command)) is False


@pytest.mark.parametrize(
    "text",
    [
        "password: " + _VALUE,
        "db.password=" + _VALUE,
        "DB_PASS=" + _VALUE,
        '{"adminPassword": "' + _VALUE + '"}',
        "server:\n  port: 80\n  auth:\n    token: " + _VALUE + "\n",
        '{"user": "app", "mode": "fast", "password": "' + _VALUE + '"}',
        '{"db": {"host": "a", "password": "' + _VALUE + '"}}',
        '{"user": "app", "password": ",,,,"}',
        "password: ',;}]'",
        "password=[" + _VALUE + "]",
        "db.password={" + _VALUE + "}",
        "MODE=fast DB_PASS=" + _VALUE + " ./run",
        "export MODE=fast; export DB_PASS=" + _VALUE,
        "password: |\n  " + _VALUE + "\n",
        "password: " + _VALUE + ", more",
    ],
    ids=[
        "yaml",
        "properties",
        "shell",
        "json",
        "nested-yaml",
        "json-later-field",
        "json-nested",
        "json-commas",
        "yaml-punctuation",
        "properties-bracket",
        "properties-brace",
        "shell-second-assignment",
        "shell-second-export",
        "yaml-block-text",
        "yaml-comma",
    ],
)
@pytest.mark.parametrize("field", ["data", "stringData"])
def test_file_text_in_a_config_map_with_a_secret_line_counts(field: str, text: str) -> None:
    manifest = _manifest(
        {"metadata": {"name": "c"}, field: {"application.yaml": text}}, "ConfigMap"
    )
    assert _flagged(manifest) is True


@pytest.mark.parametrize(
    "text",
    [
        "port: 80\nmode: fast\n",
        "password:\n",
        "token: ''",
        "# password is set elsewhere",
        "x",
        '{"user": "app", "password": ""}',
        "password:\n  file: /etc/pw\n",
        "credentials: {file: /etc/pw}",
        "credentials:\n  - file: /etc/pw\n",
        "MODE=fast LOG=debug ./run",
        "password: |\n",
    ],
)
def test_file_text_without_a_secret_line_does_not_count(text: str) -> None:
    manifest = _manifest(
        {"metadata": {"name": "c"}, "data": {"application.yaml": text}}, "ConfigMap"
    )
    assert _flagged(manifest) is False


def test_the_last_applied_annotation_is_read() -> None:
    last = json.dumps(
        {"kind": "Widget", "spec": {"settings": {"adminPassword": _VALUE}}},
    )
    manifest = _manifest(
        {"metadata": {"annotations": {"kubectl.kubernetes.io/last-applied-configuration": last}}}
    )
    assert _flagged(manifest) is True


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_every_json_annotation_is_read_whatever_its_position(position: str) -> None:
    plain = json.dumps({"s": {"t": [{"note": "x"}]}})
    secret = json.dumps({"s": {"t": [{"password": _VALUE}]}})
    index = {"first": 0, "middle": 100, "last": 199}[position]
    parts = [
        {"metadata": {"annotations": {"example.invalid/a": secret if i == index else plain}}}
        for i in range(200)
    ]
    assert _flagged(_manifest({"metadata": {"name": "w"}, "spec": {"parts": parts}})) is True


def test_an_annotation_text_that_is_not_json_is_a_plain_text() -> None:
    manifest = _manifest({"metadata": {"annotations": {"note": "{not json", "n2": "[1, 2"}}})
    assert _flagged(manifest) is False


_SHARED_MAP_ORDERS = {
    "binary-data-first": "binaryData: &m {app.yaml: 'password: " + _VALUE + "'}\ndata: *m\n",
    "data-first": "data: &m {app.yaml: 'password: " + _VALUE + "'}\nbinaryData: *m\n",
    "binary-data-then-string-data": "binaryData: &m {app.yaml: 'password: "
    + _VALUE
    + "'}\nstringData: *m\n",
    "string-data-first": "stringData: &m {app.yaml: 'password: " + _VALUE + "'}\nbinaryData: *m\n",
}


@pytest.mark.parametrize("fields", list(_SHARED_MAP_ORDERS.values()), ids=list(_SHARED_MAP_ORDERS))
def test_a_map_shared_by_binary_data_and_data_has_its_file_text_read(fields: str) -> None:
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n" + fields
    assert _flagged(manifest) is True


def _counted(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    """The first argument of each call of *name* in the check."""
    calls: list[Any] = []
    real = getattr(op_classes, name)

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(op_classes, name, counting)
    return calls


def _shared_inputs_manifest(containers: int) -> str:
    rest = "\n".join(
        f"        - {{name: c{i}, image: i, env: *env, command: *cmd, args: *args}}"
        for i in range(1, containers)
    )
    return (
        "apiVersion: apps/v1\nkind: Deployment\n"
        "metadata: {name: w, annotations: &ann {a: '{\"x\": 1}', b: '{\"y\": 2}'}}\n"
        "spec:\n"
        "  config: {data: &d {f: 'mode: fast'}}\n"
        "  copy: {data: *d, stringData: *d, binaryData: *d}\n"
        "  template:\n"
        "    metadata: {annotations: *ann}\n"
        "    spec:\n"
        "      containers:\n"
        "        - {name: c0, image: i, env: &env [{name: A, value: '1'}, {name: B, value: '2'}],"
        " command: &cmd [run, --mode], args: &args [fast]}\n" + rest + "\n"
    )


def test_each_shared_input_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    pair_reads = _counted(monkeypatch, "_is_secret_pair")
    command_reads = _counted(monkeypatch, "hide_command_parts_from")
    json_reads = _counted(monkeypatch, "_json_objects")
    text_reads = _counted(monkeypatch, "_file_text_holds_secret")

    assert _flagged(_shared_inputs_manifest(containers=50)) is False

    # One env list of two entries, one command and one args list, two
    # annotation texts, one file text: each read once, whatever the number
    # of containers.
    env_entries = [read for read in pair_reads if "value" in read]
    assert len(env_entries) == 2
    assert len(command_reads) == 2
    assert len(json_reads) == 2
    assert len(text_reads) == 1


def test_a_shared_command_is_read_once_with_different_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_reads = _counted(monkeypatch, "hide_command_parts_from")
    containers = "\n".join(
        f"    - {{name: c{i}, image: i, command: *cmd, args: [--mode, m{i}]}}" for i in range(1, 50)
    )
    long_command = "[" + ", ".join(f"w{i}" for i in range(500)) + "]"
    manifest = (
        "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n"
        f"    - {{name: c0, image: i, command: &cmd {long_command}, args: [--mode, m0]}}\n"
        + containers
        + "\n"
    )

    assert _flagged(manifest) is False

    parts_read = sum(len(parts) for parts in command_reads)
    assert parts_read == 500 + 50 * 2


def test_a_secret_flag_at_the_end_of_a_shared_command_hides_the_first_of_each_args() -> None:
    manifest = (
        "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n"
        "    - {name: a, image: i, command: &cmd [mysql, --password], args: ['-h']}\n"
        "    - {name: b, image: i, command: *cmd, args: [" + _VALUE + "]}\n"
    )
    assert _flagged(manifest) is True


_SECRET_ANNOTATIONS = '{example.invalid/a: \'{"password": "' + _VALUE + "\"}'}"

# The walk reads the last key of an object first. In the first form it meets
# the map as an annotation value first; in the second, as annotations first.
_ANNOTATION_MAP_ORDERS = {
    "annotation-value-read-first": "metadata: {name: w, annotations: &m "
    + _SECRET_ANNOTATIONS
    + "}\nspec: {a: {annotations: {k: *m}}}\n",
    "annotations-read-first": "spec: {a: {annotations: {k: &m "
    + _SECRET_ANNOTATIONS
    + "}}}\nmetadata: {name: w, annotations: *m}\n",
}


@pytest.mark.parametrize(
    "body", list(_ANNOTATION_MAP_ORDERS.values()), ids=list(_ANNOTATION_MAP_ORDERS)
)
def test_an_annotation_map_also_used_as_an_annotation_value_has_its_texts_read(
    body: str,
) -> None:
    manifest = "apiVersion: example.invalid/v1\nkind: Widget\n" + body
    assert _flagged(manifest) is True


# ---------------------------------------------------------------------------
# Wrong inner types never raise and never skip the walk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"metadata": "text", "spec": {"data": "text"}},
        {"metadata": ["a"], "spec": {"data": [1, 2], "env": {"a": 1}, "args": 5}},
        {"metadata": {"annotations": ["a", 1]}, "data": None, "stringData": 5},
        {"metadata": {"annotations": {"a": 1, "b": None, "c": [1]}}, "env": "x", "command": None},
        {1: {2: 3}, "spec": {None: [{True: 1.5}]}},
    ],
)
def test_wrong_inner_types_do_not_raise(body: dict[str, Any]) -> None:
    assert _flagged(_manifest(body)) is False


@pytest.mark.parametrize(
    "body",
    [
        {"metadata": "text", "spec": {"data": "text", "extra": {"password": _VALUE}}},
        {"metadata": ["a"], "spec": {"env": {"a": 1}, "args": 5}, "adminPassword": _VALUE},
        {"metadata": {"annotations": ["a", {"password": _VALUE}]}},
        {
            "data": None,
            "stringData": 5,
            "spec": [{"env": [None, {"name": "DB_PASS", "value": "x"}]}],
        },
    ],
)
def test_wrong_inner_types_do_not_skip_the_walk(body: dict[str, Any]) -> None:
    assert _flagged(_manifest(body)) is True


# ---------------------------------------------------------------------------
# The object limit and speed
# ---------------------------------------------------------------------------


def test_the_object_limit_still_holds() -> None:
    items = ", ".join("{kind: Widget, metadata: {name: a}}" for _ in range(MAX_CHECKED_OBJECTS + 1))
    assert _flagged(f"apiVersion: v1\nkind: List\nitems: [{items}]\n") is True


def test_a_large_plain_manifest_stays_fast() -> None:
    entries = [
        {"name": f"SETTING_{i}", "value": f"v{i}", "labels": {"a": "b"}} for i in range(30_000)
    ]
    manifest = _manifest({"metadata": {"name": "big"}, "spec": {"entries": entries}})

    started = time.perf_counter()
    assert _flagged(manifest) is False
    assert time.perf_counter() - started < 30


def test_a_deeply_nested_json_annotation_does_not_raise() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    manifest = _manifest({"metadata": {"annotations": {"a": deep}}})
    assert isinstance(_flagged(manifest), bool)


# ---------------------------------------------------------------------------
# Plain, real-looking manifests: the count that now hit
# ---------------------------------------------------------------------------

_PLAIN_MANIFESTS = [
    # The name is a plain word, the value too.
    "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\ndata: {mode: fast, url: 'https://x.invalid/a'}\n",
    "apiVersion: v1\nkind: Service\nmetadata: {name: web, labels: {app: web}}\nspec: {ports: [{port: 80}], selector: {app: web}}\n",
    "apiVersion: v1\nkind: Namespace\nmetadata: {name: apps, labels: {team: a}}\n",
    "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: sa}\nautomountServiceAccountToken: false\nimagePullSecrets: [{name: registry}]\n",
    "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: web, annotations: {nginx.ingress.kubernetes.io/rewrite-target: /}}\nspec: {tls: [{hosts: [a.invalid], secretName: web-tls}], rules: [{host: a.invalid}]}\n",
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web}\nspec:\n  replicas: 2\n  selector: {matchLabels: {app: web}}\n  template:\n    metadata: {labels: {app: web}}\n    spec:\n      containers:\n        - name: web\n          image: example/web:1\n          command: [/bin/web]\n          args: [--port, '8080', --log-level=debug]\n          env:\n            - {name: LOG_LEVEL, value: debug}\n            - {name: PORT, value: '8080'}\n            - name: DB_PASSWORD\n              valueFrom: {secretKeyRef: {name: db-login, key: password}}\n          volumeMounts: [{name: tls, mountPath: /tls}]\n      volumes:\n        - {name: tls, secret: {secretName: web-tls}}\n",
    "apiVersion: batch/v1\nkind: CronJob\nmetadata: {name: nightly}\nspec:\n  schedule: '0 2 * * *'\n  jobTemplate:\n    spec:\n      template:\n        spec:\n          restartPolicy: Never\n          containers:\n            - {name: j, image: example/j:1, command: [sh, -c, 'echo done; date']}\n",
    "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: nginx}\ndata:\n  nginx.conf: |\n    server {\n      listen 80;\n      location / { proxy_pass http://web:8080; }\n    }\n",
    "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: app}\ndata:\n  application.yaml: |\n    server:\n      port: 8080\n    logging:\n      level: info\n    features:\n      keyboardShortcuts: true\n",
    "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: data}\nspec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}\n",
    "apiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata: {name: reader}\nrules: [{apiGroups: [''], resources: [pods, secrets], verbs: [get, list]}]\n",
    "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata: {name: web}\nspec: {minReplicas: 1, maxReplicas: 5, scaleTargetRef: {kind: Deployment, name: web, apiVersion: apps/v1}}\n",
    "apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: deny}\nspec: {podSelector: {}, policyTypes: [Ingress]}\n",
    "apiVersion: apps/v1\nkind: StatefulSet\nmetadata: {name: db}\nspec:\n  serviceName: db\n  template:\n    spec:\n      containers:\n        - name: db\n          image: example/db:1\n          env:\n            - {name: POSTGRES_DB, value: app}\n            - {name: PGDATA, value: /data}\n            - {name: TZ, value: UTC}\n",
    "apiVersion: cert-manager.io/v1\nkind: Certificate\nmetadata: {name: web}\nspec: {secretName: web-tls, dnsNames: [a.invalid], issuerRef: {name: ca, kind: ClusterIssuer}}\n",
]


def test_plain_real_looking_manifests_are_not_flagged() -> None:
    hits = [i for i, manifest in enumerate(_PLAIN_MANIFESTS) if _flagged(manifest)]
    assert len(_PLAIN_MANIFESTS) == 15
    assert hits == []


_NAMES_WITH_A_SECRET_WORD = [
    # A secret word inside a longer name, with a plain value: the wider name
    # rule counts these, by design.
    _pod({"env": [{"name": "KEYBOARD_LAYOUT", "value": "us"}]}),
    _pod({"env": [{"name": "AUTHOR", "value": "team"}]}),
    _manifest({"metadata": {"name": "w"}, "spec": {"passwordSecretRef": "db-login"}}),
    _pod({"args": ["--keyfile", "/etc/key"]}),
    _pod({"env": [{"name": "PUBLIC_KEY_PATH", "value": "/etc/key.pub"}]}),
]


def test_names_with_a_secret_word_and_a_plain_value_are_counted() -> None:
    """Of 20 real-looking manifests, the 15 above are not flagged and these 5 are."""
    assert len(_NAMES_WITH_A_SECRET_WORD) == 5
    assert all(_flagged(manifest) for manifest in _NAMES_WITH_A_SECRET_WORD)


# ---------------------------------------------------------------------------
# The stored request keeps no copy: before approval, after approval, after the end
# ---------------------------------------------------------------------------


def _config_map(name: str, text: str) -> str:
    return _manifest({"metadata": {"name": "c"}, "data": {name: text}}, "ConfigMap")


_STORED_FORMS = {
    "spec-data": _in_place("spec-data", "password", _VALUE),
    "annotation": _in_place("annotation", "adminPassword", _VALUE),
    "label": _in_place("label", "api_key", _VALUE),
    "json-annotation": _in_place("json-annotation", "client-secret", _VALUE),
    "env-name": _pod({"env": [{"name": "PGPASSWORD", "value": _VALUE}]}),
    "args": _pod({"args": ["--token", _VALUE]}),
    "file-text": _manifest({"metadata": {"name": "c"}, "data": {"a.yaml": "password: " + _VALUE}}),
    "command-then-args": _pod({"command": ["mysql", "--password"], "args": [_VALUE]}),
    "json-file-later-field": _manifest(
        {
            "metadata": {"name": "c"},
            "data": {"a.json": '{"user": "app", "password": "' + _VALUE + '"}'},
        }
    ),
    "json-file-commas": _manifest(
        {
            "metadata": {"name": "c"},
            "data": {"a.json": '{"user": "app", "password": "' + _COMMAS + '"}'},
        }
    ),
    "binary-data-shares-data": "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n"
    + _SHARED_MAP_ORDERS["binary-data-first"],
    "properties-bracket": _config_map("app.properties", "password=[" + _VALUE + "]"),
    "shell-second-assignment": _config_map("run.sh", "MODE=fast DB_PASS=" + _VALUE + " ./run"),
    "yaml-block-text": _config_map("app.yaml", "db:\n  password: |\n    " + _VALUE + "\n"),
    "json-in-a-custom-field": _manifest(
        {"metadata": {"name": "w"}, "spec": {"config": json.dumps({"token": _VALUE})}}
    ),
    "http-header": _pod(
        {"livenessProbe": {"httpGet": {"httpHeaders": [{"name": "X-Token", "value": _VALUE}]}}}
    ),
    **{
        f"annotation-map-{name}": "apiVersion: example.invalid/v1\nkind: Widget\n" + body
        for name, body in _ANNOTATION_MAP_ORDERS.items()
    },
}


def _reviewer() -> Operator:
    return Operator(
        sub="op:reviewer",
        name="Reviewer",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["waiting", "approved", "rejected"])
@pytest.mark.parametrize("manifest", list(_STORED_FORMS.values()), ids=list(_STORED_FORMS))
async def test_the_stored_request_keeps_no_copy(manifest: str, stage: str) -> None:
    params: dict[str, Any] = {"manifest": manifest}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_service(),
            connector_id="kubernetes-1.x",
            op_id="k8s.apply",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
        )
        if stage == "approved":
            request.status = ApprovalRequestStatus.APPROVED.value
            request.reviewed_by = "op:reviewer"
            request.decided_at = datetime.now(UTC)
        await s.commit()
        request_id = request.id
    if stage == "rejected":
        async with get_sessionmaker()() as s:
            await reject_request(s, request_id, operator=_reviewer(), reason="no")
            await s.commit()

    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT, request_id=request_id)
    stored = json.dumps({"params": row.params, "parent": row.resume_parent})
    assert _VALUE not in stored
    assert _COMMAS not in stored


async def _store_with_preview(
    manifest: str, preview: dict[str, Any], stage: str
) -> tuple[dict[str, Any], str]:
    """Store a request with its preview at *stage*; the stored effect and the whole row as JSON."""
    params: dict[str, Any] = {"manifest": manifest}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_service(),
            connector_id="kubernetes-1.x",
            op_id="k8s.apply",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect={"op_id": "k8s.apply", "preview": preview},
        )
        if stage == "approved":
            request.status = ApprovalRequestStatus.APPROVED.value
            request.reviewed_by = "op:reviewer"
            request.decided_at = datetime.now(UTC)
        await s.commit()
        request_id = request.id
    if stage == "rejected":
        async with get_sessionmaker()() as s:
            await reject_request(s, request_id, operator=_reviewer(), reason="no")
            await s.commit()

    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT, request_id=request_id)
    stored = json.dumps(
        {"effect": row.proposed_effect, "params": row.params, "parent": row.resume_parent}
    )
    return row.proposed_effect, stored


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["waiting", "approved", "rejected"])
@pytest.mark.parametrize("manifest", list(_STORED_FORMS.values()), ids=list(_STORED_FORMS))
async def test_the_stored_preview_keeps_no_copy(
    monkeypatch: pytest.MonkeyPatch, manifest: str, stage: str
) -> None:
    assert _flagged(manifest) is True
    preview = await _apply_preview(monkeypatch, manifest)

    effect, stored = await _store_with_preview(manifest, preview, stage)

    assert effect["preview"]["manifest"]
    assert _VALUE not in stored
    assert _COMMAS not in stored


def test_the_preview_keeps_an_ordinary_json_annotation_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    text = '{"command":["date"],"env":[{"name":"MODE","value":"fast"}]}'
    manifest = _manifest({"metadata": {"name": "w", "annotations": {"example.invalid/run": text}}})
    preview = asyncio.run(_apply_preview(monkeypatch, manifest))
    assert preview["manifest"][0]["metadata"]["annotations"] == {"example.invalid/run": text}


def test_the_preview_hides_only_the_secret_value_of_a_json_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    text = '{"command":["date"],"password":"' + _VALUE + '","port":80}'
    manifest = _manifest({"metadata": {"name": "w", "annotations": {"example.invalid/run": text}}})
    preview = asyncio.run(_apply_preview(monkeypatch, manifest))
    shown = preview["manifest"][0]["metadata"]["annotations"]["example.invalid/run"]
    assert _VALUE not in shown
    # The same keys in the same order and spacing; only the secret is replaced.
    assert shown.startswith('{"command":["date"],"password":"')
    assert shown.endswith('","port":80}')


@pytest.mark.parametrize(
    "text,kept",
    [
        ("password=[" + _VALUE + "]\nport=80\n", ["port=80\n"]),
        ('{"user": "app", "password": ",,,,"}', ['{"user": "app", "password": "', '"}']),
        ("MODE=fast DB_PASS=" + _VALUE + "\n", ["MODE=fast DB_PASS="]),
        ("db:\n  password: |\n    " + _VALUE + "\n  port: 80\n", ["db:\n", "  port: 80\n"]),
    ],
    ids=["properties", "json", "shell", "yaml-block"],
)
def test_the_preview_hides_the_secret_values_of_a_config_map_file(
    monkeypatch: pytest.MonkeyPatch, text: str, kept: list[str]
) -> None:
    import asyncio

    preview = asyncio.run(_apply_preview(monkeypatch, _config_map("app.conf", text)))
    shown = preview["manifest"][0]["data"]["app.conf"]
    assert _VALUE not in shown
    assert ",,,," not in shown
    assert all(part in shown for part in kept)


def test_the_preview_keeps_a_plain_config_map_file_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    text = "port: 80\nmode: fast\ncredentials:\n  file: /etc/pw\n"
    preview = asyncio.run(_apply_preview(monkeypatch, _config_map("app.yaml", text)))
    assert preview["manifest"][0]["data"] == {"app.yaml": text}


_NUMBER = 918273645

_NUMBER_FORMS = {
    "custom-resource": _manifest(
        {"metadata": {"name": "w"}, "spec": {"settings": {"adminPassword": _NUMBER}}}
    ),
    "top": _manifest({"metadata": {"name": "w"}, "password": _NUMBER}),
    "float": _manifest({"metadata": {"name": "w"}, "spec": {"apiToken": float(_NUMBER)}}),
    "annotation-json": _manifest(
        {
            "metadata": {
                "name": "w",
                "annotations": {"example.invalid/last": json.dumps({"password": _NUMBER})},
            }
        }
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["waiting", "approved", "rejected"])
@pytest.mark.parametrize("manifest", list(_NUMBER_FORMS.values()), ids=list(_NUMBER_FORMS))
async def test_the_stored_preview_hides_a_number_under_a_secret_name(
    monkeypatch: pytest.MonkeyPatch, manifest: str, stage: str
) -> None:
    assert _flagged(manifest) is True
    preview = await _apply_preview(monkeypatch, manifest)
    assert str(_NUMBER) not in json.dumps(preview)

    params: dict[str, Any] = {"manifest": manifest}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=_service(),
            connector_id="kubernetes-1.x",
            op_id="k8s.apply",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect={"op_id": "k8s.apply", "preview": preview},
        )
        if stage == "approved":
            request.status = ApprovalRequestStatus.APPROVED.value
            request.reviewed_by = "op:reviewer"
            request.decided_at = datetime.now(UTC)
        await s.commit()
        request_id = request.id
    if stage == "rejected":
        async with get_sessionmaker()() as s:
            await reject_request(s, request_id, operator=_reviewer(), reason="no")
            await s.commit()

    async with get_sessionmaker()() as s:
        row = await get_request(s, tenant_id=_TENANT, request_id=request_id)
    assert row.proposed_effect["preview"]["manifest"]
    stored = json.dumps(
        {"effect": row.proposed_effect, "params": row.params, "parent": row.resume_parent}
    )
    assert str(_NUMBER) not in stored


def test_the_preview_keeps_a_plain_number_and_a_secret_setting_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    manifest = _manifest(
        {"metadata": {"name": "w"}, "spec": {"replicas": 3, "passwordRequired": True}}
    )
    preview = asyncio.run(_apply_preview(monkeypatch, manifest))
    spec = preview["manifest"][0]["spec"]
    assert spec == {"replicas": 3, "passwordRequired": True}


@pytest.mark.parametrize("manifest", list(_STORED_FORMS.values()), ids=list(_STORED_FORMS))
def test_the_clean_up_of_a_row_that_holds_the_manifest_removes_it(manifest: str) -> None:
    hidden = params_with_secret_fields_hidden({"manifest": manifest}, op_id="k8s.apply")
    assert hidden is not None
    assert _VALUE not in json.dumps(hidden)
    assert _COMMAS not in json.dumps(hidden)


def test_the_clean_up_leaves_a_plain_row_alone() -> None:
    plain = {"manifest": _PLAIN_MANIFESTS[0]}
    assert params_with_secret_fields_hidden(plain, op_id="k8s.apply") is None


def test_the_check_is_declared_for_the_stored_param() -> None:
    assert op_classes.manifest_holds_credential is manifest_holds_credential
