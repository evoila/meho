# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DIAGNOSTIC aggregator (branch diag/ci-lifespan-timing only — DO NOT MERGE).

Reads the per-worker timing files written by the env-gated instrumentation in
conftest.py and prints two attribution tables to stdout for the #771 CI
wall-time investigation:

* lifespan-step timing  — which FastAPI lifespan step eats the per-test cost.
* fixture-setup timing  — which fixture's setup the --durations report blames
  on ``test_mcp_*`` setup.
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
