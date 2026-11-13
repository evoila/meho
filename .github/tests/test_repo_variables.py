# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""The workflows read some values from repository variables.

These tests check what each workflow does when a variable is set and when
it is not set. They read the workflow files, work out the GitHub
expressions for both cases, and run the small scripts that use the values.

Run: uv run --no-project --with pytest --with pyyaml pytest .github/tests
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"

PROXY = "proxy.example.test/hub"


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


# --- ci.yml: test images and the spec shelf ------------------------------


def _ci_jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = _load("ci.yml")["jobs"]
    return jobs


def _proxy_image_envs() -> list[tuple[str, str, str]]:
    found = []
    for job_id, job in _ci_jobs().items():
        for key, value in (job.get("env") or {}).items():
            if "vars.CI_IMAGE_PROXY" in str(value):
                found.append((job_id, key, str(value)))
    return found


def test_ci_has_test_images_that_use_the_proxy() -> None:
    assert len(_proxy_image_envs()) >= 10


@pytest.mark.parametrize(("job_id", "key", "value"), _proxy_image_envs())
def test_ci_test_image_uses_the_proxy_when_set(job_id: str, key: str, value: str) -> None:
    image = render(value, vars={"CI_IMAGE_PROXY": PROXY})
    assert image.startswith(PROXY + "/"), (job_id, key, image)
    assert "docker.io" not in image


@pytest.mark.parametrize(("job_id", "key", "value"), _proxy_image_envs())
def test_ci_test_image_falls_back_when_unset(job_id: str, key: str, value: str) -> None:
    image = render(value, vars={})
    configured = render(value, vars={"CI_IMAGE_PROXY": PROXY})
    if key == "MEHO_TEST_KEYCLOAK_IMAGE":
        # No public fallback: an empty image makes the Keycloak test skip.
        assert image == "", (job_id, image)
    else:
        assert image == "docker.io/" + configured.removeprefix(PROXY + "/"), (job_id, key)


def _shelf_jobs() -> list[str]:
    return [
        job_id
        for job_id, job in _ci_jobs().items()
        if "SPEC_SHELF_TOKEN_PRESENT" in (job.get("env") or {})
    ]


def test_ci_has_jobs_that_check_out_the_shelf() -> None:
    assert len(_shelf_jobs()) >= 2


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
    secrets = {"SPEC_SHELF_TOKEN": secret} if secret else {}
    variables = {"SPEC_SHELF_REPOSITORY": repo} if repo else {}
    env = _env(job["env"], vars=variables, secrets=secrets)
    shelf_steps = [
        step
        for step in job["steps"]
        if "SPEC_SHELF" in json.dumps(step) and "SPEC_SHELF_TOKEN_PRESENT" in str(step.get("if"))
    ]
    checkout = [s for s in shelf_steps if "vars.SPEC_SHELF_REPOSITORY" in json.dumps(s)]
    assert checkout, f"{job_id}: no shelf checkout step"
    for step in shelf_steps:
        assert run_if(step["if"], env=env, vars=variables) is runs, (job_id, step.get("name"))
    if runs:
        rendered = render(checkout[0]["with"]["repository"], vars=variables)
        assert rendered == repo


# --- security-scan.yml: the scan image ----------------------------------


def _semgrep_image() -> str:
    for job in _load("security-scan.yml")["jobs"].values():
        image = (job.get("container") or {}).get("image", "")
        if "semgrep" in str(image):
            return str(image)
    raise AssertionError("no semgrep container image in security-scan.yml")


def test_security_scan_image_uses_the_proxy_when_set() -> None:
    image = render(_semgrep_image(), vars={"CI_IMAGE_PROXY": PROXY})
    assert image.startswith(PROXY + "/semgrep/semgrep:")


def test_security_scan_image_falls_back_to_docker_hub_when_unset() -> None:
    assert render(_semgrep_image(), vars={}).startswith("semgrep/semgrep:")


# --- runner-smoke.yml: the whole check depends on the proxy -------------


def _runner_smoke() -> dict[str, Any]:
    return _load("runner-smoke.yml")


@pytest.mark.parametrize(("proxy", "runs"), [(PROXY, True), ("", False)])
def test_runner_smoke_runs_only_when_proxy_is_set(proxy: str, runs: bool) -> None:
    variables = {"CI_IMAGE_PROXY": proxy} if proxy else {}
    assert run_if(_runner_smoke()["jobs"]["smoke"]["if"], vars=variables) is runs


@pytest.mark.parametrize(("ca_name", "runs"), [("Example Root CA", True), ("", False)])
def test_runner_smoke_ca_step_runs_only_when_ca_name_is_set(ca_name: str, runs: bool) -> None:
    flow = _runner_smoke()
    variables = {"CI_IMAGE_PROXY": PROXY, **({"CI_PROXY_CA_NAME": ca_name} if ca_name else {})}
    env = _env(flow["env"], vars=variables)
    ca_steps = [
        s for s in flow["jobs"]["smoke"]["steps"] if "CI_PROXY_CA_NAME" in str(s.get("run"))
    ]
    assert ca_steps
    for step in ca_steps:
        assert run_if(step["if"], env=env, vars=variables) is runs


def test_runner_smoke_derives_proxy_values(tmp_path: Path) -> None:
    flow = _runner_smoke()
    step = next(
        s for s in flow["jobs"]["smoke"]["steps"] if s.get("name") == "Derive the proxy values"
    )
    github_env = tmp_path / "github_env"
    env = {
        **_env(flow["env"], vars={"CI_IMAGE_PROXY": PROXY}),
        "GITHUB_ENV": str(github_env),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["bash", "-c", step["run"]], env=env, check=True)
    values = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert values == {
        "PROXY_HOST": "proxy.example.test",
        "SMOKE_IMAGE_PROJECT_PREFIXED": f"{PROXY}/library/alpine:3.20",
        "PROXY_REPO_PATH": "hub/library/alpine",
        "PROXY_API_URL": "https://proxy.example.test/api/v2.0",
    }


# --- pr-smoke.yml: the OIDC audience has no built-in default ------------


def _pr_smoke_job() -> dict[str, Any]:
    jobs: dict[str, dict[str, Any]] = _load("pr-smoke.yml")["jobs"]
    for job in jobs.values():
        if "OIDC_AUDIENCE" in (job.get("env") or {}):
            return job
    raise AssertionError("no job with OIDC_AUDIENCE in pr-smoke.yml")


@pytest.mark.parametrize("audience", ["example-audience", ""])
def test_pr_smoke_audience_comes_only_from_the_variable(audience: str) -> None:
    variables = {"RKE2_OIDC_AUDIENCE": audience} if audience else {}
    env = _env(_pr_smoke_job()["env"], vars=variables)
    assert env["OIDC_AUDIENCE"] == audience


def _run_oidc_script(audience: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    step = next(s for s in _pr_smoke_job()["steps"] if "getIDToken" in str(s.get("with")))
    harness = (
        "const calls = {failed: null, requested: null, exported: {}};\n"
        "const core = {\n"
        "  setFailed: (m) => { calls.failed = m; },\n"
        "  getIDToken: async (a) => { calls.requested = a; return 'id-token'; },\n"
        "  setSecret: () => {},\n"
        "  exportVariable: (k, v) => { calls.exported[k] = v; },\n"
        "};\n"
        "(async () => {\n" + step["with"]["script"] + "\n})()"
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


def test_pr_smoke_oidc_step_fails_clearly_when_audience_is_unset() -> None:
    calls = _run_oidc_script("")
    assert "RKE2_OIDC_AUDIENCE" in calls["failed"]
    assert calls["requested"] is None
    assert calls["exported"] == {}


def test_pr_smoke_oidc_step_requests_a_token_for_the_set_audience() -> None:
    calls = _run_oidc_script("example-audience")
    assert calls["failed"] is None
    assert calls["requested"] == "example-audience"
    assert calls["exported"] == {"K8S_BEARER_TOKEN": "id-token"}
