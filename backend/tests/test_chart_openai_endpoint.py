# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Chart values for an OpenAI-compatible model endpoint (#5851).

``agent.openai`` sets the three backplane settings ``openai_api_key``,
``openai_base_url`` and ``openai_default_model`` through the env vars
``OPENAI_API_KEY``, ``OPENAI_BASE_URL`` and ``OPENAI_DEFAULT_MODEL``.

These tests check that:

* without the new values the render is the same as before: no
  ``OPENAI_*`` env, no new object, and the same output when the block is
  filled in but turned off;
* the key comes only from a ``secretKeyRef``, from a Secret the operator
  names or the ExternalSecret the chart makes;
* the chart refuses a plain key and a missing Secret.

``test_chart_bring_your_own_render.py`` also compares a full render with
a saved copy, so any change to the default render shows there too. CI's
``.github/workflows/chart.yml`` checks the same wiring on the rendered
manifest. Skips where ``helm`` is absent unless ``MEHO_REQUIRE_HELM=1``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHART_DIR = _REPO_ROOT / "deploy" / "charts" / "meho"

_BASE_OVERRIDES = [
    "--namespace",
    "default",
    "--set",
    "corePostgres.enabled=false",
    "--set",
    "coreKeycloak.enabled=false",
    "--set",
    "coreVault.enabled=false",
    "--set",
    "image.tag=test",
    "--set",
    "ingress.host=meho.test",
    "--set",
    "ingress.tls.secretName=meho-tls",
    "--set",
    "postgres.credentialsSecret=meho-postgres",
    "--set",
    "vault.address=https://vault.test",
    "--set",
    "keycloak.issuer=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakIssuerUrl=https://keycloak.test/realms/meho",
    "--set",
    "config.keycloakAudience=meho-backplane",
    "--set",
    "config.vaultAddr=https://vault.test",
    "--set",
    "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set",
    "networkPolicy.vaultCIDR=10.0.2.0/24",
    "--set",
    "networkPolicy.keycloakCIDR=10.0.3.0/24",
]

_BASE_URL = "https://model.example.internal/v1"
_MODEL = "openai:meta-llama/Llama-3.1-8B-Instruct"
_OPENAI_ENV = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_DEFAULT_MODEL")

# The chart makes the broadcast password and the metrics token at random
# on every render when no Secret is given. Mask them before comparing.
_RANDOM_VALUES = (
    re.compile(r'^(\s+requirepass: )"[^"]*"$', re.MULTILINE),
    re.compile(r'^(\s+token: )"[^"]*"$', re.MULTILINE),
    re.compile(r"^(\s+checksum/metrics-auth: )[0-9a-f]+$", re.MULTILINE),
)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)


def _helm_template(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test", str(_CHART_DIR), *_BASE_OVERRIDES, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _render(*extra: str) -> str:
    result = _helm_template(*extra)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _mask_random(rendered: str) -> str:
    for pattern in _RANDOM_VALUES:
        rendered = pattern.sub(r"\1<random>", rendered)
    return rendered


def _docs(rendered: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(rendered) if d]


def _backplane_env(rendered: str) -> dict[str, dict[str, Any]]:
    for doc in _docs(rendered):
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "test-meho":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {e["name"]: e for e in container["env"]}
    raise AssertionError("backplane Deployment not rendered")


def _enabled(*extra: str) -> list[str]:
    return ["--set", "agent.openai.enabled=true", *extra]


def test_default_render_sets_nothing_new() -> None:
    rendered = _render()
    for name in _OPENAI_ENV:
        assert name not in rendered
    assert "agent-openai" not in rendered


def test_filled_in_but_off_renders_the_same_as_the_default() -> None:
    off = _render(
        "--set",
        "agent.openai.enabled=false",
        "--set",
        "agent.openai.secretName=model-creds",
        "--set",
        f"agent.openai.baseUrl={_BASE_URL}",
        "--set",
        f"agent.openai.defaultModel={_MODEL}",
        "--set",
        "eso.agentOpenai.enabled=false",
        "--set",
        "eso.agentOpenai.remoteKey=secret/meho/model",
    )
    assert _mask_random(off) == _mask_random(_render())


def test_enabled_sets_the_three_settings_with_the_key_from_a_secret() -> None:
    env = _backplane_env(
        _render(
            *_enabled(
                "--set",
                "agent.openai.secretName=model-creds",
                "--set",
                f"agent.openai.baseUrl={_BASE_URL}",
                "--set",
                f"agent.openai.defaultModel={_MODEL}",
            )
        )
    )
    assert env["OPENAI_API_KEY"] == {
        "name": "OPENAI_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "model-creds", "key": "api_key"}},
    }
    assert env["OPENAI_BASE_URL"] == {"name": "OPENAI_BASE_URL", "value": _BASE_URL}
    assert env["OPENAI_DEFAULT_MODEL"] == {"name": "OPENAI_DEFAULT_MODEL", "value": _MODEL}


def test_empty_base_url_and_model_are_not_rendered() -> None:
    env = _backplane_env(_render(*_enabled("--set", "agent.openai.secretName=model-creds")))
    assert "secretKeyRef" in env["OPENAI_API_KEY"]["valueFrom"]
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_DEFAULT_MODEL" not in env


def test_secret_key_is_configurable() -> None:
    env = _backplane_env(
        _render(
            *_enabled(
                "--set",
                "agent.openai.secretName=model-creds",
                "--set",
                "agent.openai.secretKey=token",
            )
        )
    )
    assert env["OPENAI_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "model-creds",
        "key": "token",
    }


def test_external_secrets_path_makes_and_uses_its_own_secret() -> None:
    rendered = _render(
        *_enabled(
            "--set",
            "eso.agentOpenai.enabled=true",
            "--set",
            "eso.secretStore.name=vault",
        )
    )
    env = _backplane_env(rendered)
    assert env["OPENAI_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "test-meho-agent-openai",
        "key": "api_key",
    }
    external = [
        d
        for d in _docs(rendered)
        if d["kind"] == "ExternalSecret" and d["metadata"]["name"] == "test-meho-agent-openai"
    ]
    assert len(external) == 1
    spec = external[0]["spec"]
    assert spec["target"]["name"] == "test-meho-agent-openai"
    assert spec["data"] == [
        {
            "secretKey": "api_key",
            "remoteRef": {"key": "secret/meho/agent-openai", "property": "api_key"},
        }
    ]


def test_external_secrets_path_next_to_the_anthropic_one() -> None:
    rendered = _render(
        *_enabled(
            "--set",
            "eso.agentOpenai.enabled=true",
            "--set",
            "eso.agent.enabled=true",
            "--set",
            "eso.secretStore.name=vault",
        )
    )
    names = sorted(d["metadata"]["name"] for d in _docs(rendered) if d["kind"] == "ExternalSecret")
    assert names == ["test-meho-agent", "test-meho-agent-openai"]


def test_enabled_without_a_secret_fails() -> None:
    result = _helm_template(*_enabled())
    assert result.returncode != 0
    assert "agent.openai.enabled=true requires either agent.openai.secretName" in result.stderr


def test_external_secrets_path_without_a_store_fails() -> None:
    result = _helm_template(*_enabled("--set", "eso.agentOpenai.enabled=true"))
    assert result.returncode != 0
    assert "eso.secretStore.name is required" in result.stderr


def test_a_plain_key_value_is_refused() -> None:
    result = _helm_template(
        *_enabled(
            "--set",
            "agent.openai.secretName=model-creds",
            "--set",
            "agent.openai.apiKey=not-a-real-key",
        )
    )
    assert result.returncode != 0
    assert "apiKey" in result.stderr


def test_base_url_must_be_a_url() -> None:
    result = _helm_template(
        *_enabled(
            "--set",
            "agent.openai.secretName=model-creds",
            "--set",
            "agent.openai.baseUrl=model.example.internal",
        )
    )
    assert result.returncode != 0
    assert "agent.openai.baseUrl" in result.stderr


def test_helm_test_pod_reads_the_key_from_the_same_secret() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "test",
            str(_CHART_DIR),
            *_BASE_OVERRIDES,
            *_enabled(
                "--set",
                "agent.openai.secretName=model-creds",
                "--set",
                f"agent.openai.baseUrl={_BASE_URL}",
            ),
            "--show-only",
            "templates/tests/test-agent-openai-config.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    pod = yaml.safe_load(result.stdout)
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    env = {e["name"]: e for e in pod["spec"]["containers"][0]["env"]}
    assert env["OPENAI_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "model-creds",
        "key": "api_key",
    }
    assert env["OPENAI_BASE_URL"]["value"] == _BASE_URL
    assert "OPENAI_DEFAULT_MODEL" not in env


def test_helm_test_pod_absent_by_default() -> None:
    assert "test-agent-openai-config" not in _render()
