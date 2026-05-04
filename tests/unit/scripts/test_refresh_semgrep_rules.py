# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for ``scripts/refresh-semgrep-rules.py``.

Loads the hyphenated script via ``importlib.util`` because its filename is
not a legal Python module name. Tests the pure curation primitives
(``_is_dropped``, ``_route_rule``, ``classify_rules``) that determine which
registry rules end up in the vendored snapshot. Network calls
(``_download``) and filesystem writes (``main``'s output writes) are not
exercised here -- the locking integration test is "run the script + CI
passes against the produced rules," covered in CI.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "refresh-semgrep-rules.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("refresh_semgrep_rules", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> types.ModuleType:
    return _load_script()


class TestLanguagePrefix:
    def test_returns_first_dot_segment(self, script: types.ModuleType) -> None:
        assert script._language_prefix("python.foo.bar.baz") == "python"

    def test_no_dots_returns_whole_string(self, script: types.ModuleType) -> None:
        assert script._language_prefix("standalone") == "standalone"

    def test_empty_string(self, script: types.ModuleType) -> None:
        assert script._language_prefix("") == ""


class TestIsDropped:
    @pytest.mark.parametrize(
        "rule_id",
        [
            "java.spring.x.y",
            "go.lang.x.y",
            "ruby.lang.x.y",
            "php.laravel.x.y",
            "terraform.aws.x.y",
            "csharp.dotnet.x.y",
        ],
    )
    def test_drops_off_stack_first_segment(self, script: types.ModuleType, rule_id: str) -> None:
        assert script._is_dropped(rule_id) is True

    @pytest.mark.parametrize(
        "rule_id",
        [
            "python.django.x.y",
            "javascript.express.x.y",
            "typescript.node.x.y",
            "generic.nginx.x.y",
            "generic.secrets.x.y",
            "yaml.kubernetes.x.y",
            "dockerfile.security.x.y",
            "bash.foo.x.y",
            "json.x.y",
            "html.x.y",
        ],
    )
    def test_keeps_on_stack_first_segment(self, script: types.ModuleType, rule_id: str) -> None:
        assert script._is_dropped(rule_id) is False

    def test_drops_problem_based_packs_with_off_stack_sublang(
        self, script: types.ModuleType
    ) -> None:
        rule_id = "problem-based-packs.insecure-transport.java-stdlib.httpget.x"
        assert script._is_dropped(rule_id) is True

    def test_keeps_problem_based_packs_with_on_stack_sublang(
        self, script: types.ModuleType
    ) -> None:
        rule_id = "problem-based-packs.insecure-transport.js-node.bypass-tls.x"
        assert script._is_dropped(rule_id) is False

    def test_drops_generic_visualforce(self, script: types.ModuleType) -> None:
        """Major #4 from the PR review — visualforce rules are off-stack."""
        rule_id = "generic.visualforce.security.x.y"
        assert script._is_dropped(rule_id) is True

    def test_keeps_generic_nginx(self, script: types.ModuleType) -> None:
        """nginx is in MEHO's stack; the broad generic.* keep should retain it."""
        rule_id = "generic.nginx.security.x.y"
        assert script._is_dropped(rule_id) is False

    def test_drop_lang_prefixes_match_each_other(self, script: types.ModuleType) -> None:
        """Documents the contract that every entry in DROP_LANGS triggers a drop.

        Catches a future maintainer who edits the set but introduces a typo
        (e.g. trailing whitespace in an entry).
        """
        for lang in script.DROP_LANGS:
            assert script._is_dropped(f"{lang}.foo") is True


class TestRouteRule:
    @pytest.mark.parametrize(
        ("rule_id", "expected"),
        [
            ("python.django.x", "python.yml"),
            ("javascript.express.x", "frontend.yml"),
            ("typescript.node.x", "frontend.yml"),
            ("html.x.y", "frontend.yml"),
            ("generic.x.y", "cross-cutting.yml"),
            ("yaml.kubernetes.x", "cross-cutting.yml"),
            ("dockerfile.security.x", "cross-cutting.yml"),
            ("bash.x", "cross-cutting.yml"),
            ("json.x", "cross-cutting.yml"),
        ],
    )
    def test_routes_kept_languages_to_correct_file(
        self, script: types.ModuleType, rule_id: str, expected: str
    ) -> None:
        assert script._route_rule(rule_id) == expected

    def test_routes_problem_based_packs_to_cross_cutting(self, script: types.ModuleType) -> None:
        assert (
            script._route_rule("problem-based-packs.insecure-transport.js-node.x")
            == "cross-cutting.yml"
        )

    def test_unrecognized_prefix_returns_none(self, script: types.ModuleType) -> None:
        # `sql` is not in KEEP_LANGS, DROP_LANGS, or `problem-based-packs`.
        assert script._route_rule("sql.injection.x") is None


class TestClassifyRules:
    """Integration of dedup + filter + route across multiple packs."""

    def test_dedupes_across_packs_first_seen_wins(self, script: types.ModuleType) -> None:
        raw_packs = {
            "python": [{"id": "python.foo.x", "message": "from python pack"}],
            "typescript": [],
            "security-audit": [{"id": "python.foo.x", "message": "from security-audit"}],
            "owasp-top-ten": [],
        }
        routed, dropped, unrecognized = script.classify_rules(raw_packs)
        assert len(routed["python.yml"]) == 1
        assert routed["python.yml"][0]["message"] == "from python pack"
        assert dropped == 0
        assert unrecognized == []

    def test_dropped_count_includes_off_stack_languages(self, script: types.ModuleType) -> None:
        raw_packs = {
            "python": [],
            "typescript": [],
            "security-audit": [
                {"id": "java.spring.x", "message": "java rule"},
                {"id": "go.lang.x", "message": "go rule"},
                {"id": "python.kept.x", "message": "kept"},
            ],
            "owasp-top-ten": [],
        }
        routed, dropped, unrecognized = script.classify_rules(raw_packs)
        assert dropped == 2  # java + go
        assert len(routed["python.yml"]) == 1
        assert unrecognized == []

    def test_unrecognized_prefix_is_surfaced_not_dropped(self, script: types.ModuleType) -> None:
        """Major #2 from the PR review — silent drop becomes a surfaced error."""
        raw_packs = {
            "python": [{"id": "python.normal.x", "message": "kept"}],
            "typescript": [],
            "security-audit": [
                {"id": "newcategory.foo.x", "message": "registry added a new family"}
            ],
            "owasp-top-ten": [],
        }
        routed, dropped, unrecognized = script.classify_rules(raw_packs)
        assert dropped == 0
        assert unrecognized == ["newcategory.foo.x"]
        assert len(routed["python.yml"]) == 1

    def test_unrecognized_list_is_sorted(self, script: types.ModuleType) -> None:
        """Sorted output means deterministic error messages on repeat runs."""
        raw_packs = {
            "python": [],
            "typescript": [],
            "security-audit": [
                {"id": "zzz.foo.x", "message": ""},
                {"id": "aaa.foo.x", "message": ""},
                {"id": "mmm.foo.x", "message": ""},
            ],
            "owasp-top-ten": [],
        }
        _, _, unrecognized = script.classify_rules(raw_packs)
        assert unrecognized == ["aaa.foo.x", "mmm.foo.x", "zzz.foo.x"]

    def test_skips_empty_or_missing_rule_ids(self, script: types.ModuleType) -> None:
        raw_packs = {
            "python": [
                {"id": "", "message": "empty id"},
                {"message": "missing id"},
                {"id": "python.real.x", "message": "real rule"},
            ],
            "typescript": [],
            "security-audit": [],
            "owasp-top-ten": [],
        }
        routed, dropped, unrecognized = script.classify_rules(raw_packs)
        assert len(routed["python.yml"]) == 1
        assert dropped == 0
        assert unrecognized == []


class TestFilterConstantsCoverage:
    """Lightweight coherence checks on the constant sets.

    The script's behavior is contract-driven by these sets; a typo or
    accidental empty drop here would silently change what gets vendored.
    """

    def test_keep_and_drop_are_disjoint(self, script: types.ModuleType) -> None:
        assert script.KEEP_LANGS.isdisjoint(script.DROP_LANGS)

    def test_route_keys_subset_of_keep_langs(self, script: types.ModuleType) -> None:
        # Every routable prefix must be in the KEEP set — otherwise it'd never
        # reach the routing stage.
        assert set(script.ROUTE.keys()).issubset(script.KEEP_LANGS)

    def test_route_values_are_known_filenames(self, script: types.ModuleType) -> None:
        expected = {"python.yml", "frontend.yml", "cross-cutting.yml"}
        assert set(script.ROUTE.values()) == expected

    def test_drop_sublang_tokens_end_with_dash(self, script: types.ModuleType) -> None:
        """Tokens are matched as substrings against `problem-based-packs.*`
        rule IDs; the trailing dash prevents false-positives like 'java' inside
        'javascript-foo'."""
        for token in script.DROP_SUBLANG_TOKENS:
            assert token.endswith("-"), f"token {token!r} must end with '-'"
