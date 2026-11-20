# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent's copy of a Kubernetes span hides secret env values and command-line secrets.

Covers :mod:`meho_backplane.connectors.kubernetes.trace_agent_view` and
:mod:`meho_backplane.flight_recorder.agent_view`:

* an env value is hidden by the same secret rules as in the direct answer
  of an operation: a secret-named entry is hidden, a plain value stays, and
  the password in a database URL that the stored trace hides stays hidden;
  the env names and a ``valueFrom`` reference stay;
* ``--password x`` on a command line and a token in an ``sh -c`` script are
  hidden; the other words stay;
* the last-applied annotation, which holds a whole object, is cleaned like
  the object; other annotation values are checked for secret shapes;
* ``managedFields`` stays;
* the same rules hold for Deployments, CronJobs, lists and custom resources;
* nothing else in the span changes, and the span of a non-Kubernetes call is
  copied unchanged;
* the walk takes time in line with the body size and runs in a worker thread.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

import pytest

import meho_backplane.flight_recorder.agent_view as agent_view_mod
from meho_backplane.connectors.kubernetes.trace_agent_view import (
    LAST_APPLIED_MARKER,
    agent_view,
)
from meho_backplane.redaction.command_line import HIDDEN
from meho_backplane.redaction.flight_recorder._content import scrub_content

_FAKE = "made-up-value-not-real-8812"
_URL_FAKE = "made-up-url-value-not-real-5521"
#: What the secret rules of the direct answer put in place of a hidden value.
_HIDDEN_ENV_VALUE = "[REDACTED:param_name]"
_TOKEN = "ghp_" + "A" * 36
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _container(**extra: Any) -> dict[str, Any]:
    return {
        "name": "app",
        "image": "registry.example.invalid/app:1.0",
        "env": [
            {
                "name": "DATABASE_URL",
                "value": f"postgres://app:{_URL_FAKE}@db.example.invalid/app",
            },
            {"name": "DB_PASSWORD", "value": _FAKE},
            {"name": "LOG_LEVEL", "value": "debug"},
            {
                "name": "API_KEY",
                "valueFrom": {"secretKeyRef": {"name": "app-secrets", "key": "api-key"}},
            },
        ],
        "envFrom": [{"configMapRef": {"name": "app-config"}}],
        **extra,
    }


def _pod(**container_extra: Any) -> dict[str, Any]:
    return {
        "kind": "Pod",
        "apiVersion": "v1",
        "metadata": {
            "name": "app-0",
            "namespace": "default",
            "uid": "uid-1",
            "labels": {"app": "demo"},
            "annotations": {"team": "platform"},
            "managedFields": [
                {
                    "manager": "kubectl",
                    "operation": "Update",
                    "fieldsV1": {"f:spec": {"f:containers": {}}},
                }
            ],
        },
        "spec": {"containers": [_container(**container_extra)], "nodeName": "node-1"},
        "status": {"phase": "Running"},
    }


def _k8s_span(body: Any) -> dict[str, Any]:
    return {
        "connector_id": "k8s-1.x",
        "op_id": "k8s.pod.list",
        "method": "GET",
        "url": "https://cluster.example.invalid/api/v1/namespaces/default/pods",
        "request_headers": {"accept": "application/json"},
        "response_headers": {"content-type": "application/json"},
        "request_body": None,
        "response_body": body,
        "body_recorded": True,
        "response_object": {
            "kind": "PodList",
            "item_count": 1,
            "managedFields": [{"manager": "kubectl"}],
        },
    }


def _env(view: dict[str, Any]) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = view["spec"]["containers"][0]["env"]
    return env


# --------------------------------------------------------------------------
# env values
# --------------------------------------------------------------------------


def test_env_value_with_password_in_url_is_hidden_and_names_stay() -> None:
    # The stored trace has been through the capture-time pass.
    stored, _ = scrub_content(_pod())

    view = agent_view(stored)

    env = _env(view)
    assert [entry["name"] for entry in env] == [
        "DATABASE_URL",
        "DB_PASSWORD",
        "LOG_LEVEL",
        "API_KEY",
    ]
    assert _URL_FAKE not in env[0]["value"]
    assert env[0]["value"].startswith("postgres://")
    assert env[0]["value"].endswith("@db.example.invalid/app")
    assert env[1]["value"] != _FAKE
    assert env[2]["value"] == "debug"
    # A value from a reference keeps the reference.
    assert env[3] == {
        "name": "API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "app-secrets", "key": "api-key"}},
    }
    assert view["spec"]["containers"][0]["envFrom"] == [{"configMapRef": {"name": "app-config"}}]
    assert _FAKE not in json.dumps(view)
    assert _URL_FAKE not in json.dumps(view)


def test_env_value_that_is_empty_or_not_text_follows_the_direct_answer_rules() -> None:
    pod = _pod()
    pod["spec"]["containers"][0]["env"] = [
        {"name": "EMPTY", "value": ""},
        {"name": "NUMBER", "value": 5},
        {"name": "NULL", "value": None},
        {"name": "PIN_PASSWORD", "value": 1234},
        {"name": "CONFIG", "value": f"password: {_FAKE}"},
        "not-an-entry",
    ]

    env = _env(agent_view(pod))

    # Plain values stay; a number under a secret name and a secret field
    # inside a text are hidden.
    assert env[:4] == [
        {"name": "EMPTY", "value": ""},
        {"name": "NUMBER", "value": 5},
        {"name": "NULL", "value": None},
        {"name": "PIN_PASSWORD", "value": _HIDDEN_ENV_VALUE},
    ]
    assert env[4]["name"] == "CONFIG"
    assert _FAKE not in env[4]["value"]
    assert env[4]["value"].startswith("password:")
    assert env[5] == "not-an-entry"


# --------------------------------------------------------------------------
# command lines and scripts
# --------------------------------------------------------------------------


def test_password_flag_on_a_command_line_is_hidden_and_the_words_stay() -> None:
    pod = _pod(command=["/app/server"], args=["--password", _FAKE, "--port", "8080"])

    container = agent_view(pod)["spec"]["containers"][0]

    assert container["command"] == ["/app/server"]
    assert container["args"] == ["--password", HIDDEN, "--port", "8080"]


def test_flag_at_the_end_of_command_hides_the_first_arg() -> None:
    pod = _pod(command=["/app/server", "--token"], args=[_FAKE, "--verbose"])

    container = agent_view(pod)["spec"]["containers"][0]

    assert container["args"] == [HIDDEN, "--verbose"]


def test_token_in_an_inline_script_is_hidden_and_the_script_stays_readable() -> None:
    script = f"export API_TOKEN={_FAKE}; curl -sf https://api.example.invalid/x && echo {_TOKEN}"
    pod = _pod(command=["sh", "-c"], args=[script])

    args = agent_view(pod)["spec"]["containers"][0]["args"]

    assert _FAKE not in args[0]
    assert _TOKEN not in args[0]
    assert args[0].startswith(f"export API_TOKEN={HIDDEN};")
    assert "curl -sf https://api.example.invalid/x" in args[0]


def test_probe_exec_command_gets_the_same_pass() -> None:
    probe = {"exec": {"command": ["check", f"--password={_FAKE}"]}}
    pod = _pod(livenessProbe=probe)

    container = agent_view(pod)["spec"]["containers"][0]

    assert container["livenessProbe"]["exec"]["command"] == ["check", f"--password={HIDDEN}"]


# --------------------------------------------------------------------------
# annotations and managedFields
# --------------------------------------------------------------------------


def test_last_applied_annotation_is_cleaned_like_the_object() -> None:
    earlier = _pod(command=["sh", "-c"], args=[f"PGPASSWORD={_FAKE} psql"])
    pod = _pod()
    pod["metadata"]["annotations"][_LAST_APPLIED] = json.dumps(earlier) + "\n"

    view = agent_view(pod)

    text = view["metadata"]["annotations"][_LAST_APPLIED]
    assert _FAKE not in text
    cleaned = json.loads(text)
    assert _env(cleaned)[1] == {"name": "DB_PASSWORD", "value": _HIDDEN_ENV_VALUE}
    assert _env(cleaned)[2] == {"name": "LOG_LEVEL", "value": "debug"}
    assert cleaned["metadata"]["managedFields"] == earlier["metadata"]["managedFields"]
    assert cleaned["spec"]["containers"][0]["args"] == [f"PGPASSWORD={HIDDEN} psql"]
    assert cleaned["spec"]["nodeName"] == "node-1"
    assert view["metadata"]["annotations"]["team"] == "platform"


def test_last_applied_annotation_that_is_not_json_is_replaced() -> None:
    pod = _pod()
    pod["metadata"]["annotations"][_LAST_APPLIED] = '{"spec": {"cut'

    view = agent_view(pod)

    assert view["metadata"]["annotations"][_LAST_APPLIED] == LAST_APPLIED_MARKER


def test_other_annotation_values_are_checked_for_secret_shapes() -> None:
    pod = _pod()
    pod["metadata"]["annotations"].update(
        {
            "example.invalid/backup-target": f"s3://backup:{_FAKE}@store.example.invalid/b",
            "example.invalid/bot": f"token {_TOKEN}",
            "example.invalid/config": json.dumps(
                {"env": [{"name": "X_TOKEN", "value": _FAKE}, {"name": "X", "value": "plain"}]}
            ),
        }
    )

    annotations = agent_view(pod)["metadata"]["annotations"]

    assert annotations["example.invalid/backup-target"] == (
        f"s3://backup:{HIDDEN}@store.example.invalid/b"
    )
    assert annotations["example.invalid/bot"] == f"token {HIDDEN}"
    assert json.loads(annotations["example.invalid/config"]) == {
        "env": [{"name": "X_TOKEN", "value": _HIDDEN_ENV_VALUE}, {"name": "X", "value": "plain"}]
    }


@pytest.mark.parametrize(
    "value",
    [
        {"message": f"deployed by token {_TOKEN}"},
        ["first note", f"token {_TOKEN}", {"url": f"https://bot:{_FAKE}@git.example.invalid"}],
    ],
    ids=["object", "list"],
)
def test_texts_inside_a_json_annotation_are_checked_for_secret_shapes(value: Any) -> None:
    pod = _pod()
    pod["metadata"]["annotations"]["example.invalid/notes"] = json.dumps(value)
    pod["metadata"]["annotations"][_LAST_APPLIED] = json.dumps(value)

    annotations = agent_view(pod)["metadata"]["annotations"]

    for name in ("example.invalid/notes", _LAST_APPLIED):
        text = annotations[name]
        assert _TOKEN not in text
        assert _FAKE not in text
        assert HIDDEN in text
        assert json.loads(text)  # still valid JSON


def test_managed_fields_stay_in_body_and_summary() -> None:
    span = _k8s_span({"kind": "PodList", "items": [_pod()]})

    attributes = agent_view_mod.agent_view_attributes(span)

    pod = attributes["response_body"]["items"][0]
    assert (
        pod["metadata"]["managedFields"]
        == span["response_body"]["items"][0]["metadata"]["managedFields"]
    )
    assert attributes["response_object"] == span["response_object"]


# --------------------------------------------------------------------------
# other workload kinds
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wrap", "path"),
    [
        (
            lambda pod: {"kind": "Deployment", "spec": {"template": {"spec": pod["spec"]}}},
            ("spec", "template", "spec"),
        ),
        (
            lambda pod: {
                "kind": "CronJob",
                "spec": {"jobTemplate": {"spec": {"template": {"spec": pod["spec"]}}}},
            },
            ("spec", "jobTemplate", "spec", "template", "spec"),
        ),
        (
            lambda pod: {"kind": "StatefulSetList", "items": [{"spec": {"template": pod}}]},
            ("items", 0, "spec", "template", "spec"),
        ),
        (
            lambda pod: {
                "apiVersion": "example.invalid/v1",
                "kind": "Workflow",
                "spec": {"steps": [{"podSpec": pod["spec"]}]},
            },
            ("spec", "steps", 0, "podSpec"),
        ),
    ],
    ids=["deployment", "cronjob", "list", "custom-resource"],
)
def test_pod_templates_in_other_kinds_are_cleaned(wrap: Any, path: tuple[Any, ...]) -> None:
    body = wrap(_pod(args=["--password", _FAKE]))

    node: Any = agent_view(body)
    for step in path:
        node = node[step]

    container = node["containers"][0]
    assert container["env"][1] == {"name": "DB_PASSWORD", "value": _HIDDEN_ENV_VALUE}
    assert container["env"][2] == {"name": "LOG_LEVEL", "value": "debug"}
    assert container["args"] == ["--password", HIDDEN]


# --------------------------------------------------------------------------
# nothing else changes
# --------------------------------------------------------------------------


def test_only_the_listed_values_change_in_a_kubernetes_span() -> None:
    original = _k8s_span({"kind": "PodList", "items": [_pod(args=["--password", _FAKE])]})
    before = copy.deepcopy(original)

    view = agent_view_mod.agent_view_attributes(original)

    assert original == before  # the stored span is not touched
    expected = copy.deepcopy(original)
    pod = expected["response_body"]["items"][0]
    pod["spec"]["containers"][0]["env"][1]["value"] = _HIDDEN_ENV_VALUE
    pod["spec"]["containers"][0]["args"] = ["--password", HIDDEN]
    assert view == expected


def test_a_non_kubernetes_span_is_copied_unchanged() -> None:
    span = _k8s_span({"kind": "PodList", "items": [_pod(args=["--password", _FAKE])]})
    span["connector_id"] = "vmware-rest-9.0"
    span["op_id"] = "vsphere.vm.list"

    assert agent_view_mod.agent_view_attributes(span) == span


@pytest.mark.parametrize(
    ("connector_id", "op_id", "cleaned"),
    [
        ("k8s", "k8s.pod.list", True),
        ("k8s-1.x", "k8s.pod.list", True),
        (None, "k8s.pod.list", True),
        ("k8sx-1.x", "k8sx.pod.list", False),
        ("rke2-ssh-1.x", "rke2.node.list", False),
        (None, None, False),
    ],
)
def test_kubernetes_span_is_found_by_connector_or_operation(
    connector_id: str | None, op_id: str | None, cleaned: bool
) -> None:
    span = _k8s_span(_pod())
    span["connector_id"] = connector_id
    span["op_id"] = op_id

    view = agent_view_mod.agent_view_attributes(span)

    assert (view != span) is cleaned


async def test_rows_without_a_kubernetes_span_are_returned_as_they_are() -> None:
    rows = [{"seq": 0, "attributes": {"connector_id": "vault", "response_body": {"a": 1}}}]

    assert await agent_view_mod.agent_view_rows(rows) is rows


# --------------------------------------------------------------------------
# time and the event loop
# --------------------------------------------------------------------------


_TIME_LIMIT_SECONDS = 2.0


def test_large_pod_list_is_cleaned_in_time_in_line_with_its_size() -> None:
    pods = [_pod(command=["sh", "-c"], args=[f"run --token {_FAKE} --id {i}"]) for i in range(2000)]
    span = _k8s_span({"kind": "PodList", "items": pods})

    started = time.perf_counter()
    view = agent_view_mod.agent_view_attributes(span)
    elapsed = time.perf_counter() - started

    assert elapsed < _TIME_LIMIT_SECONDS
    assert _FAKE not in json.dumps(view)
    assert len(view["response_body"]["items"]) == 2000


def test_very_long_command_line_is_cleaned_in_time() -> None:
    long_arg = "x" * (1 << 20)
    pod = _pod(command=["sh", "-c"], args=[f"{long_arg} --password {_FAKE}"])

    started = time.perf_counter()
    args = agent_view(pod)["spec"]["containers"][0]["args"]

    assert time.perf_counter() - started < _TIME_LIMIT_SECONDS
    assert args == [f"{long_arg} --password {HIDDEN}"]


async def test_the_walk_runs_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[threading.Thread] = []
    real = agent_view_mod.agent_view_attributes

    def _spy(attributes: Any) -> dict[str, Any]:
        seen.append(threading.current_thread())
        return real(attributes)

    monkeypatch.setattr(agent_view_mod, "agent_view_attributes", _spy)
    rows = [{"seq": 0, "attributes": _k8s_span(_pod(args=["--password", _FAKE]))}]

    cleaned = await agent_view_mod.agent_view_rows(rows)

    assert seen
    assert all(thread is not threading.main_thread() for thread in seen)
    container = cleaned[0]["attributes"]["response_body"]["spec"]["containers"][0]
    assert container["args"] == ["--password", HIDDEN]


def test_a_body_nested_too_deep_raises_so_the_trace_is_withheld() -> None:
    node: Any = {}
    for _ in range(5000):
        node = {"a": node}

    with pytest.raises(RecursionError):
        agent_view_mod.agent_view_attributes(_k8s_span(node))
