# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :mod:`scripts.retrieval_eval.agent_search`. Recorded answers only; no live MEHO."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.retrieval_eval import agent_search as runner
from scripts.retrieval_eval.agent_search_questions import (
    Question,
    QuestionFileError,
    load_questions,
)

TOKEN = "made-up-token-value-1234"
EXAMPLE = Path(runner.__file__).parent / "agent_search_questions_example.yaml"


def wiki_hit(document_id: str | None = None, source_url: str | None = None, **extra: Any) -> dict:
    return {"source": "wiki", "document_id": document_id, "source_url": source_url, **extra}


def store_hit(relative_path: str | None) -> dict:
    metadata = {"provenance": {"relative_path": relative_path}} if relative_path else {}
    return {"slug": "entry", "metadata": metadata}


def other(n: int) -> dict:
    return wiki_hit(f"other/{n}.md")


def make_outcome(place: int | None, kind: str = "meaning", qid: str = "q") -> runner.Outcome:
    return runner.Outcome(Question(qid, "text", "a/page.md", kind=kind), place)


def test_places_one_four_ten_and_not_found() -> None:
    expected = Question("q", "text", "a/page.md")
    for place in (1, 4, 10):
        hits = [other(n) for n in range(1, place)] + [wiki_hit("a/page.md")]
        assert runner.score_answer(expected, runner.Answer(hits, None, None)).place == place
    missing = runner.Answer([other(n) for n in range(1, 10)], None, None)
    assert runner.score_answer(expected, missing).place is None


def test_hit_after_place_ten_keeps_its_place_but_is_not_in_top_ten() -> None:
    hits = [other(n) for n in range(1, 11)] + [wiki_hit("a/page.md")]
    outcome = runner.score_answer(Question("q", "t", "a/page.md"), runner.Answer(hits, None, None))
    assert outcome.place == 11
    summary = runner.summarize("all", [outcome])
    assert (summary.top3, summary.found10) == (0, 0)
    assert summary.mrr == pytest.approx(1 / 11)


def test_summary_numbers_and_kinds_are_counted_apart() -> None:
    outcomes = [
        make_outcome(1, "meaning", "a"),
        make_outcome(4, "meaning", "b"),
        make_outcome(10, "exact", "c"),
        make_outcome(None, "exact", "d"),
    ]
    all_, meaning, exact = runner.summaries(outcomes)
    assert (all_.total, all_.top3, all_.found10) == (4, 1, 3)
    assert all_.mrr == pytest.approx((1 + 0.25 + 0.1) / 4)
    assert (meaning.total, meaning.top3, meaning.found10) == (2, 1, 2)
    assert meaning.mrr == pytest.approx(0.625)
    assert (exact.total, exact.top3, exact.found10) == (2, 0, 1)
    assert exact.mrr == pytest.approx(0.05)
    assert "found in top 10: 75%" in runner.summary_line(all_)


def test_summary_of_no_questions_is_zero() -> None:
    summary = runner.summarize("exact", [])
    assert (summary.total, summary.mrr) == (0, 0.0)
    assert "0%" in runner.summary_line(summary)


def test_wiki_hit_matches_document_id_or_source_url() -> None:
    q = Question("q", "t", "products/x/page.md")
    by_id = wiki_hit("/products/x/page.md")
    by_url = wiki_hit(None, "https://git.example/g/r/-/blob/abc123/products/x/page.md#L3")
    assert runner.hit_matches(by_id, q, None)
    assert runner.hit_matches(by_url, q, None)


def test_same_file_name_in_another_folder_is_not_a_hit() -> None:
    q = Question("q", "t", "products/x/page.md")
    assert not runner.hit_matches(wiki_hit("products/y/page.md"), q, None)
    assert not runner.hit_matches(
        wiki_hit(None, "https://h/r/blob/abc/products/y/page.md"), q, None
    )
    assert not runner.hit_matches(store_hit("products/y/page.md"), q, None)
    assert not runner.hit_matches(wiki_hit("page.md"), q, None)


def test_store_hit_matches_provenance_path() -> None:
    q = Question("q", "t", "products/x/page.md")
    assert runner.hit_matches(store_hit("products/x/page.md"), q, None)
    assert not runner.hit_matches(store_hit(None), q, None)
    assert not runner.hit_matches({"slug": "s", "metadata": None}, q, None)


def test_repo_must_match_when_the_hit_names_one() -> None:
    q = Question("q", "t", "a/page.md", repo="repo-one")
    assert runner.hit_matches(wiki_hit("a/page.md", collection="repo-one"), q, None)
    assert not runner.hit_matches(wiki_hit("a/page.md", collection="repo-two"), q, None)
    assert not runner.hit_matches(wiki_hit("a/page.md"), q, {"collection": "repo-two"})
    assert runner.hit_matches(wiki_hit("a/page.md"), q, {"collection": "repo-one"})
    assert runner.hit_matches(wiki_hit("a/page.md"), q, None)


def test_filtered_reason_and_partial_are_recorded() -> None:
    wiki = {"searched": False, "reason": "filtered", "collection": "c"}
    outcome = runner.score_answer(
        Question("q", "t", "a/page.md"), runner.Answer([store_hit("a/page.md")], wiki, "partial")
    )
    assert outcome.place == 1 and outcome.wiki_reason == "filtered" and outcome.partial
    lines = runner.extra_lines([outcome, make_outcome(None)])
    assert "knowledge repo not searched, filtered: 1" in lines
    assert "partial answers: 1" in lines


def test_parse_answer_prefers_structured_content_and_refuses_bad_bodies() -> None:
    body = {"hits": [other(1)], "delivery": "complete", "wiki": {"searched": True}}
    assert runner.parse_answer({"structuredContent": body}).hits == [other(1)]
    text = {"content": [{"type": "text", "text": json.dumps(body)}]}
    assert runner.parse_answer(text).delivery == "complete"
    for bad in ({"isError": True}, {"content": []}, {"structuredContent": {"x": 1}}):
        with pytest.raises(RuntimeError):
            runner.parse_answer(bad)


def fake_meho(answers: dict[str, dict], seen: list[dict] | None = None, *, version: Any = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json=version or {"chart_version": "9.9.9"})
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess"},
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        if seen is not None:
            seen.append({"args": body["params"]["arguments"], "headers": dict(request.headers)})
        reply = answers.get(body["params"]["arguments"]["query"])
        if reply is None:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": reply}}
        )

    return httpx.MockTransport(handler)


def write_questions(path: Path) -> Path:
    path.write_text(
        "questions:\n"
        "  - {id: a, question: first, expect: a/one.md, filters: {kind: kb-entry}}\n"
        "  - {id: b, question: second, expect: a/two.md, kind: exact}\n"
        "  - {id: c, question: third, expect: a/three.md}\n",
        encoding="utf-8",
    )
    return path


def test_main_runs_questions_and_writes_report_without_token_or_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    seen: list[dict] = []
    wiki = {"searched": False, "reason": "filtered"}
    answers = {
        "first": {"hits": [store_hit("a/one.md")], "delivery": "complete", "wiki": wiki},
        "second": {"hits": [other(1), wiki_hit("a/two.md")], "delivery": "partial"},
        "third": {"hits": [], "delivery": "complete"},
    }
    version = {"chart_version": "9.9.9", "agent_switches": {"KB_WIKI_X": True, "OTHER": False}}
    report = tmp_path / "report.md"
    argv = ["--url", "http://meho", "--questions", str(write_questions(tmp_path / "q.yaml"))]
    code = runner.main(
        [*argv, "--report", str(report)], transport=fake_meho(answers, seen, version=version)
    )
    out = capfd.readouterr()
    assert code == 0
    assert seen[0]["args"] == {"query": "first", "limit": 10, "filters": {"kind": "kb-entry"}}
    assert "filters" not in seen[1]["args"]
    assert seen[0]["headers"]["mcp-session-id"] == "sess"
    assert "all: 3 questions, top 3: 2, found in top 10: 67%" in out.out
    assert "meaning: 2 questions" in out.out and "exact: 1 questions" in out.out
    text = report.read_text(encoding="utf-8")
    assert "KB_WIKI_X: True" in text and "OTHER" not in text
    assert "9.9.9" in text and "| b | exact | hit | 2 |" in text
    for blob in (out.out, out.err, text):
        assert TOKEN not in blob
    for word in ("first", "second", "third"):
        assert word not in text


def test_report_says_not_shown_without_switches() -> None:
    text = runner.render_report([make_outcome(1)], None, "v1", datetime(2026, 1, 2, tzinfo=UTC))
    assert "not shown" in text and "2026-01-02" in text


def test_failed_call_is_an_error_not_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    answers = {"first": {"hits": [store_hit("a/one.md")]}}
    argv = ["--url", "http://meho", "--questions", str(write_questions(tmp_path / "q.yaml"))]
    code = runner.main(argv, transport=fake_meho(answers))
    out = capfd.readouterr()
    assert code == 1
    assert "b [exact] error" in out.out and "failed calls: 2" in out.out
    assert TOKEN not in out.out + out.err


def test_failed_calls_are_left_out_of_the_summary_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEHO_TOKEN", TOKEN)
    answers = {"first": {"hits": [store_hit("a/one.md")]}}
    argv = ["--url", "http://meho", "--questions", str(write_questions(tmp_path / "q.yaml"))]
    code = runner.main(argv, transport=fake_meho(answers))
    out = capfd.readouterr().out
    assert code == 1
    assert "all: 1 questions, top 3: 1, found in top 10: 100%, MRR: 1.000" in out
    assert "exact: 0 questions" in out
    assert "failed calls: 2" in out


def test_missing_token_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MEHO_TOKEN", raising=False)
    code = runner.main(
        ["--url", "http://m", "--questions", str(write_questions(tmp_path / "q.yaml"))]
    )
    assert code == 2 and "MEHO_TOKEN" in capfd.readouterr().err


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("questions:\n  - {id: a, question: q}\n", "missing field expect"),
        ("questions:\n  - {id: a, question: q, expect: e, extra: 1}\n", "unknown field extra"),
        (
            "questions:\n  - {id: a, question: q, expect: e}\n"
            "  - {id: a, question: r, expect: f}\n",
            "id used twice: a",
        ),
        ("questions:\n  - {id: a, question: q, expect: e, kind: fuzzy}\n", "kind must be one of"),
        (
            "questions:\n  - {id: a, question: q, expect: e, filters: {d: 2026-01-01}}\n",
            "cannot be sent as JSON",
        ),
        (
            "questions:\n  - {id: a, question: q, expect: e, filters: [1]}\n",
            "filters must be a mapping",
        ),
        ("questions:\n  - {id: a, question: q, expect: e, 123: x}\n", "unknown field 123"),
        (
            "questions:\n  - {id: a, question: q, expect: e, filters: {score: .nan}}\n",
            "cannot be sent as JSON",
        ),
        (
            "questions:\n  - {id: a, question: q, expect: e, filters: {score: [.inf]}}\n",
            "cannot be sent as JSON",
        ),
        ("other: 1\n", "exactly one key"),
        ("questions: []\n", "at least one"),
    ],
)
def test_broken_question_file_exits_two_and_names_problem(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], content: str, problem: str
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(content, encoding="utf-8")
    assert runner.main(["--url", "http://m", "--questions", str(path)]) == 2
    assert problem in capfd.readouterr().err


def test_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(QuestionFileError):
        load_questions(tmp_path / "none.yaml")


def test_example_file_loads() -> None:
    questions = load_questions(EXAMPLE)
    assert [q.kind for q in questions] == ["meaning", "exact"]
