# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI-perf timing report — companion to the dormant instrumentation in conftest.

Reads the per-worker JSONL files written by the env-gated hooks in
``tests/conftest.py`` (enabled via ``MEHO_LIFESPAN_TIMING`` and
``MEHO_FIXTURE_TIMING_FILE``) and prints two attribution tables:

* lifespan-step timing  — which FastAPI lifespan step dominates per-test setup.
* fixture-setup timing  — which fixture's setup the ``--durations`` report is
  really blaming.

Usage::

    MEHO_LIFESPAN_TIMING=/tmp/lifespan MEHO_FIXTURE_TIMING_FILE=/tmp/fixt \\
        uv run pytest -n 6 --dist loadscope --ignore=tests/integration tests/
    uv run python tests/_perf_timing_report.py

This is reusable diagnostic tooling, not a test (the leading underscore keeps
pytest from collecting it). It cracked the #771 unit-job wall investigation
where ``--durations`` alone could not see inside the lifespan-booting fixture.
"""

from __future__ import annotations

import collections
import glob
import json


def _load(pattern: str) -> list[dict]:
    rows: list[dict] = []
    for fpath in glob.glob(pattern):
        with open(fpath) as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _agg(rows: list[dict], key: str) -> list[tuple[str, int, float, float]]:
    acc: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    for r in rows:
        a = acc[r[key]]
        a[0] += 1
        a[1] += r["dur"]
        a[2] = max(a[2], r["dur"])
    out = [(k, int(v[0]), v[1], v[2]) for k, v in acc.items()]
    out.sort(key=lambda t: -t[2])  # by total seconds
    return out


def main() -> None:
    life = _load("/tmp/lifespan.*")
    print("=" * 78)
    print(f"LIFESPAN STEP TIMING  ({len(life)} calls)  — sorted by total seconds")
    print(f"{'step':<34}{'count':>7}{'sum_s':>11}{'max_s':>9}{'mean_s':>9}")
    for name, count, total, mx in _agg(life, "step"):
        print(f"{name:<34}{count:>7}{total:>11.1f}{mx:>9.2f}{total / count:>9.2f}")
    print()
    print("LIFESPAN — top 25 single slowest calls")
    for r in sorted(life, key=lambda row: -row["dur"])[:25]:
        print(f"{r['dur']:>8.2f}s  {r['step']:<32}{r['node']}")
    print()

    fixt = _load("/tmp/fixt.*")
    print("=" * 78)
    print(f"FIXTURE SETUP TIMING  ({len(fixt)} setups)  — top 40 by total seconds")
    print(f"{'fixture':<34}{'count':>7}{'sum_s':>11}{'max_s':>9}{'mean_s':>9}")
    for name, count, total, mx in _agg(fixt, "fix")[:40]:
        print(f"{name:<34}{count:>7}{total:>11.1f}{mx:>9.2f}{total / count:>9.2f}")


if __name__ == "__main__":
    main()
