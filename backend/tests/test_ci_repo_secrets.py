# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""The workflows read some values from repository secrets.

The secrets (CI_IMAGE_PROXY, SPEC_SHELF_REPOSITORY, RKE2_OIDC_AUDIENCE,
CI_PROXY_CA_NAME) hold internal names. They are secrets, not repository
variables: GitHub prints a step's ``env:`` and inputs, and a job's container
image, in the public log before anything runs, and only a secret shows as
``***`` there. These tests check:

* no workflow reads these names from ``vars``, and no container or service
  image is built from them (the log prints those before any step);
* in this repository an empty secret fails the job with a message that
  names it, so no test or check skips quietly;
* in another repository (a fork that runs the workflows itself) the jobs
  fall back to Docker Hub;
* only the step that needs a value gets it in its ``env:``, and that step
  hides the parts of the value that GitHub does not hide on its own.

They read the workflow files, work out the GitHub expressions, and run the
real step scripts and ``scripts/ci/repo_secrets.sh``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "repo_secrets.sh"

PROXY = "proxy.example.test/hub"
THIS_REPO = "evoila/meho"
FORK = "someone/meho"
SECRETS = ("CI_IMAGE_PROXY", "SPEC_SHELF_REPOSITORY", "RKE2_OIDC_AUDIENCE", "CI_PROXY_CA_NAME")

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)


def _load(name: str) -> dict[str, Any]:
    flow: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return flow


# --- a small evaluator for the GitHub expressions these files use ---------

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>\|\||&&|==|!=|\(|\)|,|!)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*))"
)


def _tokens(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read expression at {expr[pos:]!r}")
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _truthy(value: Any) -> bool:
    return value not in ("", False, None, 0)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


class _Expr:
    """Evaluates `||`, `&&`, `==`, `!=`, `!`, `format()` and contexts."""

    def __init__(self, expr: str, contexts: dict[str, dict[str, Any]]) -> None:
        self.toks = _tokens(expr)
        self.i = 0
        self.contexts = contexts

    def run(self) -> Any:
        value = self._or()
        if self.i != len(self.toks):
            raise ValueError(f"unread tokens: {self.toks[self.i :]}")
        return value

    def _peek(self) -> str | None:
        return self.toks[self.i][1] if self.i < len(self.toks) else None

    def _take(self) -> tuple[str, str]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def _or(self) -> Any:
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._cmp()
        while self._peek() == "&&":
            self._take()
            right = self._cmp()
            value = right if _truthy(value) else value
        return value

    def _cmp(self) -> Any:
        value = self._unary()
        if self._peek() in ("==", "!="):
            op = self._take()[1]
            right = self._unary()
            same = _text(value).lower() == _text(right).lower()
            return same if op == "==" else not same
        return value

    def _unary(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        kind, text = self._take()
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if text == "(":
            value = self._or()
            self._take()
            return value
        if text in ("true", "false"):
            return text == "true"
        if self._peek() == "(":
            return self._call(text)
        context, _, key = text.partition(".")
        return self.contexts.get(context, {}).get(key, "")

    def _call(self, func: str) -> Any:
        self._take()
        args = [self._or()]
        while self._peek() == ",":
            self._take()
            args.append(self._or())
        self._take()
        if func != "format":
            raise ValueError(f"function {func} is not supported here")
        return re.sub(r"\{(\d+)\}", lambda m: _text(args[1 + int(m.group(1))]), args[0])


def evaluate(expr: str, **contexts: dict[str, Any]) -> Any:
    return _Expr(expr, contexts).run()


def render(value: Any, **contexts: dict[str, Any]) -> str:
    """Fill each `${{ ... }}` in a workflow value, as the runner does."""
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda m: _text(evaluate(m.group(1), **contexts)),
        _text(value),
    )


def run_if(condition: Any, **contexts: dict[str, Any]) -> bool:
    text = _text(condition).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return _truthy(evaluate(text, **contexts))


def _env(block: dict[str, Any] | None, **contexts: dict[str, Any]) -> dict[str, str]:
    return {k: render(v, **contexts) for k, v in (block or {}).items()}


def test_evaluator_follows_github_rules() -> None:
    assert evaluate("vars.A || 'x'", vars={}) == "x"
    assert evaluate("vars.A || 'x'", vars={"A": "a"}) == "a"
    assert evaluate("vars.A && format('{0}/i', vars.A) || 'i'", vars={}) == "i"
    assert evaluate("vars.A && format('{0}/i', vars.A) || 'i'", vars={"A": "a"}) == "a/i"
    assert evaluate("vars.A != ''", vars={}) is False
    assert evaluate("env.A == 'TRUE'", env={"A": "true"}) is True
    assert evaluate("github.repository == 'o/r' && 'x' || 'y'", github={"repository": "o/r"}) == "x"
    assert evaluate("github.repository == 'o/r' && 'x' || 'y'", github={"repository": "f/r"}) == "y"


# --- running a step script ------------------------------------------------


def _contexts(repo: str, secrets: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {"secrets": secrets, "github": {"repository": repo}}


def _run_step(
    run: str, env: dict[str, str], tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[str]]:
    """Run a step's `run:` text as the runner does; return the exported env and masks."""
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    result = subprocess.run(
        ["bash", "-e", "-c", run],
        env={
            **env,
            "GITHUB_ENV": str(github_env),
            "GITHUB_WORKSPACE": str(_REPO_ROOT),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    exported = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    masks = [
        line.removeprefix("::add-mask::")
        for line in result.stdout.splitlines()
        if line.startswith("::add-mask::")
    ]
    return result, exported, masks


# --- ci.yml: the step that reads the secrets -----------------------------

_READ_STEP = "Read the CI repository secrets"


def _ci_jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = _load("ci.yml")["jobs"]
    return jobs


def _read_steps() -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_id, step)
        for job_id, job in _ci_jobs().items()
        for step in job.get("steps", [])
        if step.get("name") == _READ_STEP
    ]


def _read_step_ids() -> list[str]:
    return [job_id for job_id, _ in _read_steps()]


def test_ci_reads_the_secrets_in_each_job_that_uses_them() -> None:
    assert set(_read_step_ids()) == {
        "python-unit-shard",
        "python-coverage",
        "python-integration-shard",
        "python-migration-tests",
    }
    text = json.dumps(_ci_jobs())
    for job_id, job in _ci_jobs().items():
        uses = "MEHO_TEST_" in json.dumps(job.get("steps", [])) or "secrets.CI_IMAGE_PROXY" in (
            json.dumps(job)
        )
        assert uses is (job_id in _read_step_ids()), job_id
    assert "secrets.CI_IMAGE_PROXY ||" not in text


@pytest.mark.parametrize("job_id", _read_step_ids())
def test_ci_read_step_runs_right_after_checkout(job_id: str) -> None:
    steps = _ci_jobs()[job_id]["steps"]
    assert steps[0]["name"] == "Checkout"
    assert steps[1]["name"] == _READ_STEP


def _ci_step(job_id: str) -> dict[str, Any]:
    return next(step for jid, step in _read_steps() if jid == job_id)


def _ci_step_env(job_id: str, repo: str, secrets: dict[str, str]) -> dict[str, str]:
    return _env(_ci_step(job_id)["env"], **_contexts(repo, secrets))


def _all_set(job_id: str) -> dict[str, str]:
    names = [n for n in ("CI_IMAGE_PROXY", "SPEC_SHELF_REPOSITORY") if n in _ci_step(job_id)["env"]]
    values = {"CI_IMAGE_PROXY": PROXY, "SPEC_SHELF_REPOSITORY": "example-owner/spec-shelf"}
    return {n: values[n] for n in names}


@pytest.mark.parametrize("job_id", _read_step_ids())
def test_ci_test_images_use_the_proxy_and_hide_it(job_id: str, tmp_path: Path) -> None:
    secrets = _all_set(job_id)
    result, exported, masks = _run_step(
        _ci_step(job_id)["run"], _ci_step_env(job_id, THIS_REPO, secrets), tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert exported, job_id
    for key, image in exported.items():
        assert key.endswith("_IMAGE") and key.startswith(("MEHO_TEST_", "RYUK_CONTAINER_"))
        assert image.startswith(PROXY + "/"), (key, image)
    assert PROXY in masks
    assert "proxy.example.test" in masks
    if "SPEC_SHELF_REPOSITORY" in secrets:
        assert "example-owner/spec-shelf" in masks
        assert "spec-shelf" in masks


def _job_secret_pairs() -> list[tuple[str, str]]:
    return [(job_id, name) for job_id in _read_step_ids() for name in _all_set(job_id)]


@pytest.mark.parametrize(("job_id", "empty"), _job_secret_pairs())
def test_ci_empty_secret_fails_the_job_in_this_repository(
    job_id: str, empty: str, tmp_path: Path
) -> None:
    secrets = _all_set(job_id)
    del secrets[empty]
    result, exported, _ = _run_step(
        _ci_step(job_id)["run"], _ci_step_env(job_id, THIS_REPO, secrets), tmp_path
    )
    assert result.returncode != 0
    assert f"::error title=Repository secret missing::The repository secret {empty}" in (
        result.stdout
    )
    assert exported == {}


@pytest.mark.parametrize("job_id", _read_step_ids())
def test_ci_test_images_fall_back_to_docker_hub_in_another_repository(
    job_id: str, tmp_path: Path
) -> None:
    result, exported, masks = _run_step(
        _ci_step(job_id)["run"], _ci_step_env(job_id, FORK, {}), tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert masks == []
    for key, image in exported.items():
        if key in {"MEHO_TEST_KEYCLOAK_IMAGE", "MEHO_TEST_CORE_KEYCLOAK_IMAGE"}:
            # No public fallback: an empty image makes the Keycloak test skip.
            assert image == ""
        else:
            assert image.split("/", 1)[0] == "docker.io", (key, image)


def test_ci_keycloak_image_is_set_only_in_the_integration_shards(tmp_path: Path) -> None:
    job_id = "python-integration-shard"
    _, exported, _ = _run_step(
        _ci_step(job_id)["run"], _ci_step_env(job_id, THIS_REPO, _all_set(job_id)), tmp_path
    )
    assert exported["MEHO_TEST_KEYCLOAK_IMAGE"] == f"{PROXY}/keycloak/keycloak:26.0"


# --- scripts/ci/repo_secrets.sh on its own --------------------------------


def _script(
    tmp_path: Path, env: dict[str, str], stdin: str, *names: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    result = subprocess.run(
        ["bash", str(_SCRIPT), *names],
        input=stdin,
        env={**env, "GITHUB_ENV": str(github_env), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, dict(line.split("=", 1) for line in github_env.read_text().splitlines())


@pytest.mark.parametrize("proxy", ["proxy.example.test", "proxy.example.test/", "/hub"])
def test_script_refuses_a_proxy_without_host_and_project(proxy: str, tmp_path: Path) -> None:
    result, exported = _script(
        tmp_path,
        {"CI_IMAGE_PROXY": proxy, "REQUIRE_SECRETS": "true"},
        "MEHO_TEST_VALKEY_IMAGE: PROXY/valkey/valkey:8\n",
        "CI_IMAGE_PROXY",
    )
    assert result.returncode != 0
    assert "CI_IMAGE_PROXY must have the form <host>/<project>" in result.stdout
    assert exported == {}


def test_script_names_every_missing_secret(tmp_path: Path) -> None:
    result, _ = _script(
        tmp_path, {"REQUIRE_SECRETS": "true"}, "", "CI_IMAGE_PROXY", "SPEC_SHELF_REPOSITORY"
    )
    assert result.returncode != 0
    assert "secret CI_IMAGE_PROXY is empty" in result.stdout
    assert "secret SPEC_SHELF_REPOSITORY is empty" in result.stdout


def test_script_refuses_a_bad_image_line(tmp_path: Path) -> None:
    result, _ = _script(
        tmp_path, {"CI_IMAGE_PROXY": PROXY}, "MEHO_TEST_VALKEY_IMAGE: valkey/valkey:8\n"
    )
    assert result.returncode != 0
    assert "bad image line" in result.stdout


# --- ci.yml: the spec shelf -----------------------------------------------


def _shelf_jobs() -> list[str]:
    return [
        job_id for job_id, job in _ci_jobs().items() if "SPEC_SHELF_READY" in (job.get("env") or {})
    ]


def test_ci_has_jobs_that_check_out_the_shelf() -> None:
    assert len(_shelf_jobs()) >= 2
    for job_id in _shelf_jobs():
        assert "SPEC_SHELF_REPOSITORY" in _ci_step(job_id)["env"], job_id


@pytest.mark.parametrize("job_id", _shelf_jobs())
@pytest.mark.parametrize(
    ("secret", "repo", "runs"),
    [
        ("token", "example-owner/spec-shelf", True),
        ("token", "", False),
        ("", "example-owner/spec-shelf", False),
        ("", "", False),
    ],
)
def test_ci_shelf_steps_run_only_when_token_and_repository_are_set(
    job_id: str, secret: str, repo: str, runs: bool
) -> None:
    job = _ci_jobs()[job_id]
    secrets = {
        **({"SPEC_SHELF_TOKEN": secret} if secret else {}),
        **({"SPEC_SHELF_REPOSITORY": repo} if repo else {}),
    }
    env = _env(job["env"], secrets=secrets)
    shelf_steps = [
        step
        for step in job["steps"]
        if "SPEC_SHELF" in json.dumps(step) and "SPEC_SHELF_READY" in str(step.get("if"))
    ]
    checkout = [s for s in shelf_steps if "secrets.SPEC_SHELF_REPOSITORY" in json.dumps(s)]
    assert checkout, f"{job_id}: no shelf checkout step"
    for step in shelf_steps:
        assert run_if(step["if"], env=env) is runs, (job_id, step.get("name"))
    if runs:
        rendered = render(checkout[0]["with"]["repository"], secrets=secrets)
        assert rendered == repo


# --- every workflow: the values never show in clear in a log ------------
#
# GitHub prints a step's `env:` and `with:`, a job's `env:` and its container
# and service images in the log before the step or job runs, so before any
# `::add-mask::` can hide a value. Only a secret shows as `***` there.

_WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml"))
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def _workflows() -> list[str]:
    return [p.name for p in _WORKFLOW_FILES]


@pytest.mark.parametrize("name", _workflows())
def test_no_workflow_reads_the_names_from_repository_variables(name: str) -> None:
    text = (WORKFLOWS / name).read_text()
    for secret in SECRETS:
        assert f"vars.{secret}" not in text, (name, secret)
        assert f"vars['{secret}']" not in text, (name, secret)


def _images(job: dict[str, Any]) -> list[str]:
    container = job.get("container")
    images = [container if isinstance(container, str) else (container or {}).get("image", "")]
    images += [(svc or {}).get("image", "") for svc in (job.get("services") or {}).values()]
    return [str(image) for image in images if image]


@pytest.mark.parametrize("name", _workflows())
def test_no_container_or_service_image_is_built_from_the_secrets(name: str) -> None:
    for job_id, job in (_load(name).get("jobs") or {}).items():
        for image in _images(job):
            assert not any(secret in image for secret in SECRETS), (name, job_id, image)


@pytest.mark.parametrize("name", _workflows())
def test_every_expression_with_the_names_reads_the_secrets_context(name: str) -> None:
    for expr in _EXPRESSION.findall((WORKFLOWS / name).read_text()):
        for secret in SECRETS:
            for match in re.finditer(rf"([A-Za-z_]+)\.{secret}\b", expr):
                assert match.group(1) == "secrets", (name, expr.strip())


_SENTINELS = {name: f"value-of-{name.lower()}/x" for name in SECRETS}
_STEPS_THAT_READ = {_READ_STEP, "Derive the proxy values", "Configure kubectl (OIDC mode)"}


@pytest.mark.parametrize("name", _workflows())
def test_secret_values_reach_only_the_steps_that_need_them(name: str) -> None:
    flow = _load(name)
    contexts = _contexts(THIS_REPO, dict(_SENTINELS))

    def shows_a_value(block: dict[str, Any] | None) -> bool:
        for value in (block or {}).values():
            rendered = render(value, **contexts) if "secrets." in str(value) else ""
            if any(v in rendered for v in _SENTINELS.values()):
                return True
        return False

    assert not shows_a_value(flow.get("env")), name
    for job_id, job in (flow.get("jobs") or {}).items():
        assert not shows_a_value(job.get("env")), (name, job_id)
        for step in job.get("steps") or []:
            if shows_a_value(step.get("env")):
                assert step.get("name") in _STEPS_THAT_READ, (name, job_id, step.get("name"))


# --- security-scan.yml: the scan image ----------------------------------


def _semgrep_job() -> dict[str, Any]:
    jobs: dict[str, dict[str, Any]] = _load("security-scan.yml")["jobs"]
    return jobs["semgrep"]


def _semgrep_read_step() -> dict[str, Any]:
    return next(s for s in _semgrep_job()["steps"] if s.get("name") == _READ_STEP)


def test_security_scan_runs_semgrep_with_docker_run_not_a_job_container() -> None:
    job = _semgrep_job()
    assert "container" not in job
    steps = job["steps"]
    assert "actions/checkout" in steps[0]["uses"]
    assert steps[1]["name"] == _READ_STEP
    scans = [s for s in steps if "semgrep scan" in str(s.get("run"))]
    assert len(scans) == 2
    for step in scans:
        assert '"${SEMGREP_IMAGE}"' in step["run"], step["name"]
        assert str(step["run"]).startswith("docker run --rm"), step["name"]


def _semgrep_image(tmp_path: Path, repo: str, secrets: dict[str, str]) -> tuple[Any, str]:
    step = _semgrep_read_step()
    result, exported, _ = _run_step(
        step["run"], _env(step["env"], **_contexts(repo, secrets)), tmp_path
    )
    return result, exported.get("SEMGREP_IMAGE", "")


def test_security_scan_image_uses_the_proxy_when_set(tmp_path: Path) -> None:
    result, image = _semgrep_image(tmp_path, THIS_REPO, {"CI_IMAGE_PROXY": PROXY})
    assert result.returncode == 0, result.stdout + result.stderr
    assert image == f"{PROXY}/semgrep/semgrep:1.160.0"


def test_security_scan_fails_in_this_repository_when_unset(tmp_path: Path) -> None:
    result, image = _semgrep_image(tmp_path, THIS_REPO, {})
    assert result.returncode != 0
    assert "The repository secret CI_IMAGE_PROXY is empty" in result.stdout
    assert image == ""


def test_security_scan_image_falls_back_to_docker_hub_in_another_repository(
    tmp_path: Path,
) -> None:
    result, image = _semgrep_image(tmp_path, FORK, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert image == "docker.io/semgrep/semgrep:1.160.0"


# --- runner-smoke.yml: the whole check depends on the proxy -------------


def _runner_smoke() -> dict[str, Any]:
    return _load("runner-smoke.yml")


def _smoke_step(name: str) -> dict[str, Any]:
    return next(s for s in _runner_smoke()["jobs"]["smoke"]["steps"] if s.get("name") == name)


@pytest.mark.parametrize(("repo", "runs"), [(THIS_REPO, True), (FORK, False)])
def test_runner_smoke_runs_only_in_this_repository(repo: str, runs: bool) -> None:
    condition = _runner_smoke()["jobs"]["smoke"]["if"]
    assert run_if(condition, **_contexts(repo, {})) is runs


def test_runner_smoke_first_step_reads_the_secrets() -> None:
    steps = _runner_smoke()["jobs"]["smoke"]["steps"]
    assert steps[0]["name"] == "Derive the proxy values"
    assert "env" not in _runner_smoke()


def _derive(tmp_path: Path, secrets: dict[str, str]) -> tuple[Any, dict[str, str], list[str]]:
    step = _smoke_step("Derive the proxy values")
    return _run_step(step["run"], _env(step["env"], **_contexts(THIS_REPO, secrets)), tmp_path)


@pytest.mark.parametrize("empty", ["CI_IMAGE_PROXY", "CI_PROXY_CA_NAME"])
def test_runner_smoke_fails_when_a_secret_is_empty(empty: str, tmp_path: Path) -> None:
    secrets = {"CI_IMAGE_PROXY": PROXY, "CI_PROXY_CA_NAME": "Example Root CA"}
    del secrets[empty]
    result, exported, _ = _derive(tmp_path, secrets)
    assert result.returncode != 0
    assert f"The repository secret {empty} is empty" in result.stdout
    assert exported == {}


def test_runner_smoke_refuses_a_proxy_without_a_slash(tmp_path: Path) -> None:
    secrets = {"CI_IMAGE_PROXY": "proxy.example.test", "CI_PROXY_CA_NAME": "Example Root CA"}
    result, exported, _ = _derive(tmp_path, secrets)
    assert result.returncode != 0
    assert "CI_IMAGE_PROXY must have the form <host>/<project>" in result.stdout
    assert exported == {}


def test_runner_smoke_derives_and_hides_the_proxy_values(tmp_path: Path) -> None:
    secrets = {"CI_IMAGE_PROXY": PROXY, "CI_PROXY_CA_NAME": "Example Root CA"}
    result, exported, masks = _derive(tmp_path, secrets)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(masks) == {PROXY, "proxy.example.test"}
    assert exported == {
        "CI_PROXY_CA_NAME": "Example Root CA",
        "PROXY_HOST": "proxy.example.test",
        "SMOKE_IMAGE_PROJECT_PREFIXED": f"{PROXY}/library/alpine:3.20",
        "PROXY_REPO_PATH": "hub/library/alpine",
        "PROXY_API_URL": "https://proxy.example.test/api/v2.0",
    }


def test_runner_smoke_ca_step_matches_the_name_as_text() -> None:
    ca_steps = [
        s
        for s in _runner_smoke()["jobs"]["smoke"]["steps"]
        if "CI_PROXY_CA_NAME" in str(s.get("run")) and s.get("name") != "Derive the proxy values"
    ]
    assert len(ca_steps) == 1
    assert 'grep -qF -- "$CI_PROXY_CA_NAME"' in ca_steps[0]["run"]


# --- pr-smoke.yml: the OIDC audience has no built-in default ------------


def _pr_smoke_job() -> dict[str, Any]:
    jobs: dict[str, dict[str, Any]] = _load("pr-smoke.yml")["jobs"]
    return jobs["smoke"]


def _oidc_step() -> dict[str, Any]:
    return next(s for s in _pr_smoke_job()["steps"] if "getIDToken" in str(s.get("with")))


@pytest.mark.parametrize("audience", ["example-audience", ""])
def test_pr_smoke_audience_comes_only_from_the_secret(audience: str) -> None:
    secrets = {"RKE2_OIDC_AUDIENCE": audience} if audience else {}
    assert "OIDC_AUDIENCE" not in _pr_smoke_job()["env"]
    env = _env(_oidc_step()["env"], secrets=secrets)
    assert env["OIDC_AUDIENCE"] == audience


def _run_oidc_script(audience: str) -> dict[str, Any]:
    node = shutil.which("node")
    assert node is not None
    harness = (
        "const calls = {failed: null, requested: null, exported: {}, secrets: []};\n"
        "const core = {\n"
        "  setFailed: (m) => { calls.failed = m; },\n"
        "  getIDToken: async (a) => { calls.requested = a; return 'id-token'; },\n"
        "  setSecret: (s) => { calls.secrets.push(s); },\n"
        "  exportVariable: (k, v) => { calls.exported[k] = v; },\n"
        "};\n"
        "(async () => {\n" + _oidc_step()["with"]["script"] + "\n})()"
        ".then(() => console.log(JSON.stringify(calls)));\n"
    )
    result = subprocess.run(
        [node, "-e", harness],
        env={"OIDC_AUDIENCE": audience, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    loaded: dict[str, Any] = json.loads(result.stdout)
    return loaded


@requires_node
def test_pr_smoke_oidc_step_fails_clearly_when_audience_is_unset() -> None:
    calls = _run_oidc_script("")
    assert "RKE2_OIDC_AUDIENCE" in calls["failed"]
    assert calls["requested"] is None
    assert calls["exported"] == {}


@requires_node
def test_pr_smoke_oidc_step_hides_the_audience_and_requests_a_token() -> None:
    calls = _run_oidc_script("example-audience")
    assert calls["failed"] is None
    assert calls["requested"] == "example-audience"
    assert calls["secrets"] == ["example-audience", "id-token"]
    assert calls["exported"] == {"K8S_BEARER_TOKEN": "id-token"}
