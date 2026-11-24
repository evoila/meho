# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The chart renders the three knowledge search switches into the config map.

``KB_SEARCH_WORD_HINT``, ``KB_WIKI_FILTERED_SEARCH`` and
``KB_WIKI_INTERLEAVE`` are off by default. A Helm value turns each on.

Skips when ``helm`` is absent; CI sets ``MEHO_REQUIRE_HELM=1`` so there
it fails instead.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests.test_chart_knowledge_settings_values import (
    _config_map_data,
    _helm_template,
)

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None and os.environ.get("MEHO_REQUIRE_HELM") != "1",
    reason="helm not installed; set MEHO_REQUIRE_HELM=1 to fail instead of skip",
)

_SWITCHES = [
    ("kbSearchWordHint", "KB_SEARCH_WORD_HINT", "kb_search_word_hint"),
    ("kbWikiFilteredSearch", "KB_WIKI_FILTERED_SEARCH", "kb_wiki_filtered_search"),
    ("kbWikiInterleave", "KB_WIKI_INTERLEAVE", "kb_wiki_interleave"),
]


def test_defaults_render_all_three_switches_off() -> None:
    result = _helm_template()
    assert result.returncode == 0, result.stderr
    data = _config_map_data(result.stdout)
    for _value, env, _field in _SWITCHES:
        assert data[env] == "false"


@pytest.mark.parametrize(("value", "env", "field"), _SWITCHES)
def test_value_true_turns_one_switch_on(value: str, env: str, field: str) -> None:
    result = _helm_template("--set-string", f"config.{value}=true")
    assert result.returncode == 0, result.stderr
    data = _config_map_data(result.stdout)
    assert data[env] == "true"
    for _other_value, other_env, _f in _SWITCHES:
        if other_env != env:
            assert data[other_env] == "false"


@pytest.mark.parametrize(("value", "_env", "_field"), _SWITCHES)
def test_switch_takes_only_true_or_false(value: str, _env: str, _field: str) -> None:
    result = _helm_template("--set-string", f"config.{value}=maybe")
    assert result.returncode != 0
    assert value in result.stderr
