# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the made-up German and English page set and its load script.

The pages go through the import code of MEHO itself (``kb.page_import``), not
through a copy of its rules. The load script is tested with recorded answers.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.retrieval_eval import agent_search, kb_language_set_load
from scripts.retrieval_eval.agent_search_questions import Question, load_questions

from meho_backplane.kb import page_import

SET_DIR = Path(kb_language_set_load.__file__).parent / "kb_language_set"
PAGES = kb_language_set_load.read_pages(SET_DIR / "pages")
PAGE_TEXT = {page["path"]: page["content"] for page in PAGES}
QUESTION_FILES = ("questions.de.yaml", "questions.en.yaml")
TOKEN = "made-up-token-value-5678"
CODE = re.compile(r"\b[A-Z]{2,4}-[A-Z0-9]+(?:-[A-Z0-9]+)?\b")


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _body(path: str) -> str:
    return PAGE_TEXT[path].split("\n---\n", 1)[1]


def _all_questions() -> list[tuple[str, Question]]:
    return [(name, q) for name in QUESTION_FILES for q in load_questions(SET_DIR / name)]


def _import_page(path: str) -> page_import._Page:
    item = page_import.KbImportPage(path=path, content=PAGE_TEXT[path])
    return page_import._read_page(item, uuid.uuid4(), include_candidates=False)


@pytest.mark.parametrize("name", QUESTION_FILES)
def test_question_file_loads_with_the_runner_loader(name: str) -> None:
    questions = load_questions(SET_DIR / name)
    assert len(questions) == 24
    assert [q.kind for q in questions].count("meaning") == 12
    assert [q.kind for q in questions].count("exact") == 12


@pytest.mark.parametrize("name", QUESTION_FILES)
def test_question_lengths_match_their_kind(name: str) -> None:
    for question in load_questions(SET_DIR / name):
        count = len(question.question.split())
        low, high = (8, 20) if question.kind == "meaning" else (2, 4)
        assert low <= count <= high, question.id


def test_every_expect_path_is_a_page() -> None:
    for _, question in _all_questions():
        assert question.expect in PAGE_TEXT, question.id


def test_both_questions_of_a_topic_expect_the_same_page() -> None:
    for name in QUESTION_FILES:
        by_topic: dict[str, set[str]] = {}
        for question in load_questions(SET_DIR / name):
            by_topic.setdefault(question.id.rsplit("-", 1)[0], set()).add(question.expect)
        assert all(len(paths) == 1 for paths in by_topic.values())
        assert len(by_topic) == 12


def test_page_set_has_the_planned_shape() -> None:
    assert len(PAGES) == 24
    assert {path.split("/")[0] for path in PAGE_TEXT} == {"de", "en"}


@pytest.mark.parametrize("path", sorted(PAGE_TEXT))
def test_page_loads_through_the_import_code(path: str) -> None:
    page = _import_page(path)
    assert page.metadata["status"] != "candidate"
    assert page.slug == Path(path).stem
    assert 3 <= len(re.findall(r"^## ", page.body, flags=re.MULTILINE)) <= 6
    assert len(PAGE_TEXT[path]) <= page_import.MAX_PAGE_CHARS
    stored = page_import._stored_metadata(page, None, "test-actor", datetime.now(UTC))
    assert stored["provenance"]["relative_path"] == path


def test_no_two_pages_share_a_slug_and_the_request_is_within_limits() -> None:
    pages = [_import_page(path) for path in PAGE_TEXT]
    assert page_import._find_conflicts(pages) == []
    request = page_import.KbImportRequest(pages=PAGES)
    assert len(request.pages) <= page_import.MAX_PAGES


def test_runner_matches_the_stored_path_of_the_expected_page() -> None:
    for _, question in _all_questions():
        stored = page_import._stored_metadata(
            _import_page(question.expect), None, "test-actor", datetime.now(UTC)
        )
        assert agent_search.hit_matches({"metadata": stored}, question, None), question.id


def test_no_question_repeats_six_words_of_its_page() -> None:
    for _, question in _all_questions():
        page_words = _words(_body(question.expect))
        runs = {tuple(page_words[i : i + 6]) for i in range(len(page_words) - 5)}
        words = _words(question.question)
        for i in range(len(words) - 5):
            assert tuple(words[i : i + 6]) not in runs, question.id


def test_key_words_occur_in_the_expected_page() -> None:
    for _, question in _all_questions():
        if question.kind == "exact":
            body = _body(question.expect).lower()
            for word in question.question.lower().split():
                assert word in body, (question.id, word)


TWIN_PAGES = {
    "vpn": ("de/vpn-verbindung-bricht-ab.md", "en/vpn-drops-after-idle.md"),
    "print": ("de/druckauftrag-haengt-in-warteschlange.md", "en/print-job-stuck-in-queue.md"),
    "mail": ("de/postfach-voll-kontingent-ueberschritten.md", "en/mailbox-full-quota-exceeded.md"),
    "backup": (
        "de/sicherung-scheitert-an-gesperrten-dateien.md",
        "en/backup-fails-on-locked-files.md",
    ),
}


def test_key_words_of_a_two_language_topic_name_only_their_own_page() -> None:
    """A key word question needs one word that the other language's page lacks.

    Otherwise both pages match equally and the order of the two is chance.
    """
    checked = 0
    for _, question in _all_questions():
        if question.kind != "exact":
            continue
        topic = question.id.rsplit("-", 1)[0].split("-", 1)[1]
        if topic not in TWIN_PAGES:
            continue
        own = question.expect
        (twin,) = [p for p in TWIN_PAGES[topic] if p != own]
        twin_words = set(_words(_body(twin)))
        assert any(w not in twin_words for w in _words(question.question)), question.id
        checked += 1
    assert checked == 8


def test_error_codes_belong_to_one_topic() -> None:
    pages_by_code: dict[str, list[str]] = {}
    for path in PAGE_TEXT:
        for code in set(CODE.findall(_body(path))):
            pages_by_code.setdefault(code, []).append(path)
    assert pages_by_code
    assert all(len(paths) <= 2 for paths in pages_by_code.values())
    near_misses = [p for p in PAGE_TEXT if not CODE.search(_body(p))]
    assert len(near_misses) == 8


def _recording_transport(
    answer: dict[str, Any], status: int = 200
) -> tuple[Any, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=answer)

    return httpx.MockTransport(handler), seen


def _answer(
    *, dry_run: bool = False, created: int = 0, unchanged: int = 0, skipped: int = 0
) -> dict[str, Any]:
    rows = [
        {"path": "de/x.md", "slug": "x", "action": "skip", "reason": "made-up reason"}
    ] * skipped
    return {
        "dry_run": dry_run,
        "created_count": created,
        "updated_count": 0,
        "unchanged_count": unchanged,
        "skipped_count": skipped,
        "pages": rows,
        "conflicts": [],
    }


def test_load_sends_every_page_and_prints_counts(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    transport, seen = _recording_transport(_answer(created=24))
    assert kb_language_set_load.main(["--url", "http://meho.test"], transport=transport) == 0
    (request,) = seen
    body = json.loads(request.content)
    assert request.url.path == "/api/v1/kb/import"
    assert "dry_run" not in body
    assert [page["path"] for page in body["pages"]] == sorted(PAGE_TEXT)
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    output = capfd.readouterr()
    assert "created 24" in output.out
    assert TOKEN not in output.out + output.err


def test_second_run_reports_unchanged(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    transport, _ = _recording_transport(_answer(unchanged=24))
    assert kb_language_set_load.main(["--url", "http://meho.test"], transport=transport) == 0
    assert "unchanged 24" in capfd.readouterr().out


def test_dry_run_sends_dry_run_true(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    transport, seen = _recording_transport(_answer(dry_run=True, created=24))
    args = ["--url", "http://meho.test", "--dry-run"]
    assert kb_language_set_load.main(args, transport=transport) == 0
    assert json.loads(seen[0].content)["dry_run"] is True
    output = capfd.readouterr()
    assert "dry run: created 24" in output.out
    assert TOKEN not in output.out + output.err


def test_skipped_page_gives_exit_code_one(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    transport, _ = _recording_transport(_answer(created=23, skipped=1))
    assert kb_language_set_load.main(["--url", "http://meho.test"], transport=transport) == 1
    assert "skipped de/x.md: made-up reason" in capfd.readouterr().out


def test_failed_call_gives_exit_code_one_and_hides_the_token(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    transport, _ = _recording_transport({"detail": "boom"}, status=500)
    assert kb_language_set_load.main(["--url", "http://meho.test"], transport=transport) == 1
    output = capfd.readouterr()
    assert "HTTP 500" in output.err
    assert TOKEN not in output.out + output.err


def test_missing_token_gives_exit_code_two(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MEHO_TOKEN", raising=False)
    assert kb_language_set_load.main(["--url", "http://meho.test"]) == 2
    assert "MEHO_TOKEN is not set" in capfd.readouterr().err
