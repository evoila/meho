# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Measure where ``search_knowledge`` puts the expected page, as an agent sees it.

The runner asks a running MEHO each question of a question file
(:mod:`scripts.retrieval_eval.agent_search_questions`) with one MCP
``tools/call`` of ``search_knowledge``. It reads the hits in the order the
agent gets them, after MEHO has filtered and merged the store and the
knowledge repo.

Run it from ``backend/``::

    MEHO_TOKEN=... uv run python -m scripts.retrieval_eval.agent_search \\
        --url https://meho.example --questions my-questions.yaml --report out.md

The token is read from an environment variable (``--token-env``, default
``MEHO_TOKEN``). It is never printed, logged or written to the report.
Exit code: 0 when every question was asked, 1 when a call failed, 2 when
the question file or the arguments are wrong.

Test tooling only. It adds no route, setting or operation to MEHO.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

import httpx

from scripts.retrieval_eval.agent_search_questions import (
    Question,
    QuestionFileError,
    load_questions,
)

SEARCH_LIMIT: Final[int] = 10
TOP_N: Final[int] = 3
_SWITCH_WORDS: Final[tuple[str, ...]] = ("KNOWLEDGE", "KB_", "WIKI")
_NOT_SHOWN: Final[str] = "not shown"


@dataclass(frozen=True)
class Answer:
    """What one ``search_knowledge`` call returned (the parts the runner reads)."""

    hits: list[dict[str, Any]]
    wiki: dict[str, Any] | None
    delivery: str | None


@dataclass(frozen=True)
class Outcome:
    """The result for one question. ``place`` is 1-based, ``None`` when not found."""

    question: Question
    place: int | None
    wiki_reason: str | None = None
    partial: bool = False
    error: str | None = None


def _page_path_of(link: str) -> str:
    """Return the page path after ``blob/<commit>/`` in *link*, or ``""``."""
    parts = unquote(urlsplit(link).path).strip("/").split("/")
    if "blob" not in parts:
        return ""
    return "/".join(parts[parts.index("blob") + 2 :])


def _repo_name(hit: dict[str, Any], wiki: dict[str, Any] | None) -> str | None:
    name = hit.get("collection") or (wiki or {}).get("collection")
    return name if isinstance(name, str) and name else None


def hit_matches(hit: dict[str, Any], question: Question, wiki: dict[str, Any] | None) -> bool:
    """Say whether *hit* is the expected page of *question*."""
    if hit.get("source") == "wiki":
        repo = _repo_name(hit, wiki)
        if question.repo and repo and repo != question.repo:
            return False
        paths = {str(hit.get("document_id") or "").strip("/")}
        paths.add(_page_path_of(str(hit.get("source_url") or "")))
        return question.expect in paths
    metadata = hit.get("metadata")
    provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
    if not isinstance(provenance, dict):
        return False
    return str(provenance.get("relative_path") or "").strip("/") == question.expect


def score_answer(question: Question, answer: Answer) -> Outcome:
    """Find the place of the expected page among all hits the agent got."""
    place = next(
        (
            number
            for number, hit in enumerate(answer.hits, start=1)
            if hit_matches(hit, question, answer.wiki)
        ),
        None,
    )
    wiki = answer.wiki or {}
    reason = None if wiki.get("searched", True) else str(wiki.get("reason") or "unknown")
    return Outcome(question, place, reason, partial=answer.delivery == "partial")


def parse_answer(result: dict[str, Any]) -> Answer:
    """Read the ``tools/call`` result of ``search_knowledge``."""
    if result.get("isError"):
        raise RuntimeError("the tool returned an error")
    body = result.get("structuredContent")
    if not isinstance(body, dict):
        content = result.get("content") or []
        try:
            body = json.loads(content[0]["text"])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("the answer has no readable body") from exc
    hits = body.get("hits")
    if not isinstance(hits, list):
        raise RuntimeError("the answer has no hits list")
    wiki = body.get("wiki")
    return Answer(hits, wiki if isinstance(wiki, dict) else None, body.get("delivery"))


class McpSession:
    """A minimal MCP client over HTTP: one ``initialize``, then ``tools/call``."""

    def __init__(self, client: httpx.Client, token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._next_id = 0

    def _post(self, method: str, params: dict[str, Any]) -> httpx.Response:
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        response = self._client.post("/mcp", json=body, headers=self._headers)
        response.raise_for_status()
        return response

    def initialize(self) -> None:
        response = self._post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "retrieval-eval-agent-search", "version": "1"},
            },
        )
        _result_of(response)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._headers["Mcp-Session-Id"] = session_id

    def search(self, question: Question) -> Answer:
        arguments: dict[str, Any] = {"query": question.question, "limit": SEARCH_LIMIT}
        if question.filters:
            arguments["filters"] = question.filters
        response = self._post("tools/call", {"name": "search_knowledge", "arguments": arguments})
        return parse_answer(_result_of(response))


def _result_of(response: httpx.Response) -> dict[str, Any]:
    payload = response.json()
    if "error" in payload:
        message = payload["error"].get("message", "unknown error")
        raise RuntimeError(f"MCP error: {message}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("the reply has no result")
    return result


def ask_all(session: McpSession, questions: list[Question]) -> list[Outcome]:
    """Ask every question. A failed call is an error outcome, never a miss.

    The summary lines leave failed calls out; the line ``failed calls: N`` counts them.
    """
    outcomes: list[Outcome] = []
    for question in questions:
        try:
            outcomes.append(score_answer(question, session.search(question)))
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            outcomes.append(Outcome(question, None, error=f"{type(exc).__name__}: {exc}"))
    return outcomes


def fetch_switches(client: httpx.Client) -> tuple[dict[str, bool] | None, str]:
    """Read ``GET /version`` once. Return the knowledge search switches and the version."""
    try:
        response = client.get("/version")
        response.raise_for_status()
        info = response.json()
    except (httpx.HTTPError, ValueError):
        return None, _NOT_SHOWN
    shown = info.get("agent_switches")
    version = str(info.get("chart_version") or info.get("git_sha") or _NOT_SHOWN)
    if not isinstance(shown, dict):
        return None, version
    return {k: v for k, v in sorted(shown.items()) if any(w in k for w in _SWITCH_WORDS)}, version


@dataclass(frozen=True)
class Summary:
    label: str
    total: int
    top3: int
    found10: int
    mrr: float


def summarize(label: str, outcomes: list[Outcome]) -> Summary:
    """Count only the questions that got an answer. Failed calls have their own line."""
    places = [o.place for o in outcomes if o.error is None]
    return Summary(
        label=label,
        total=len(places),
        top3=sum(1 for p in places if p is not None and p <= TOP_N),
        found10=sum(1 for p in places if p is not None and p <= SEARCH_LIMIT),
        mrr=sum(1 / p for p in places if p) / len(places) if places else 0.0,
    )


def summaries(outcomes: list[Outcome]) -> list[Summary]:
    return [
        summarize("all", outcomes),
        summarize("meaning", [o for o in outcomes if o.question.kind == "meaning"]),
        summarize("exact", [o for o in outcomes if o.question.kind == "exact"]),
    ]


def summary_line(s: Summary) -> str:
    share = s.found10 / s.total if s.total else 0.0
    return (
        f"{s.label}: {s.total} questions, top 3: {s.top3}, "
        f"found in top 10: {share:.0%}, MRR: {s.mrr:.3f}"
    )


def _verdict(outcome: Outcome) -> str:
    if outcome.error:
        return "error"
    return "miss" if outcome.place is None else "hit"


def _place_text(outcome: Outcome) -> str:
    return "-" if outcome.place is None else str(outcome.place)


def question_line(outcome: Outcome) -> str:
    q = outcome.question
    text = f"{q.id} [{q.kind}] {_verdict(outcome)} place={_place_text(outcome)}"
    if outcome.wiki_reason:
        text += f" repo-not-searched={outcome.wiki_reason}"
    if outcome.error:
        text += f" ({outcome.error})"
    return text


def extra_lines(outcomes: list[Outcome]) -> list[str]:
    reasons = Counter(o.wiki_reason for o in outcomes if o.wiki_reason)
    lines = [f"knowledge repo not searched, {r}: {n}" for r, n in sorted(reasons.items())]
    lines.append(f"partial answers: {sum(1 for o in outcomes if o.partial)}")
    lines.append(f"failed calls: {sum(1 for o in outcomes if o.error)}")
    return lines


def render_report(
    outcomes: list[Outcome], switches: dict[str, bool] | None, version: str, when: datetime
) -> str:
    """Write the Markdown report. It holds ids and kinds, never the question text."""
    lines = [
        "# Agent search report",
        "",
        f"- MEHO version: {version}",
        f"- Date: {when:%Y-%m-%d}",
        "",
    ]
    lines += ["## Summary", ""]
    lines += [f"- {summary_line(s)}" for s in summaries(outcomes)]
    lines += [f"- {line}" for line in extra_lines(outcomes)]
    lines += [
        "",
        "## Questions",
        "",
        "| id | kind | result | place | reason |",
        "|---|---|---|---|---|",
    ]
    for o in outcomes:
        reason = o.wiki_reason or ""
        lines.append(
            f"| {o.question.id} | {o.question.kind} | {_verdict(o)} | {_place_text(o)} | {reason} |"
        )
    lines += ["", "## Switches", ""]
    if switches is None:
        lines.append(_NOT_SHOWN)
    else:
        lines += [f"- {name}: {value}" for name, value in switches.items()] or ["none listed"]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="address of the running MEHO")
    parser.add_argument("--questions", required=True, type=Path, help="the question file")
    parser.add_argument("--token-env", default="MEHO_TOKEN", help="env var that holds the token")
    parser.add_argument("--report", type=Path, help="write a Markdown report to this file")
    return parser


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        questions = load_questions(args.questions)
    except QuestionFileError as exc:
        print(f"question file error: {exc}", file=sys.stderr)
        return 2
    token = os.environ.get(args.token_env)
    if not token:
        print(f"environment variable {args.token_env} is not set", file=sys.stderr)
        return 2
    with httpx.Client(base_url=args.url, transport=transport, timeout=60.0) as client:
        try:
            session = McpSession(client, token)
            session.initialize()
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            print(f"cannot start an MCP session: {type(exc).__name__}", file=sys.stderr)
            return 1
        outcomes = ask_all(session, questions)
        switches, version = fetch_switches(client)
    for outcome in outcomes:
        print(question_line(outcome))
    for summary in summaries(outcomes):
        print(summary_line(summary))
    for line in extra_lines(outcomes):
        print(line)
    if args.report:
        report = render_report(outcomes, switches, version, datetime.now(UTC))
        args.report.write_text(report, encoding="utf-8")
    return 1 if any(o.error for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
