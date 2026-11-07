# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sandbox regression tests for the JSONFlux ``QueryEngine`` DuckDB connection (#101 L9).

The engine only ever loads data via in-memory Arrow tables
(``conn.register``); DuckDB is never asked to read files or URLs itself.
``QueryEngine._harden_connection`` enforces that contract with
``enable_external_access=false`` + ``allow_community_extensions=false`` +
``lock_configuration=true`` (applied in that order), closing the latent
arbitrary-file-read / SSRF / untrusted-extension surface that arbitrary SQL
would otherwise expose.

These tests assert both halves of the contract:

* the legitimate in-memory query path still works, and
* an external-access operation (reading a local file via DuckDB) is denied,
  and the sandbox cannot be unlocked at runtime.
"""

from __future__ import annotations

import duckdb
import pytest

from meho_backplane.jsonflux.query.engine import QueryEngine


def test_in_memory_query_still_works() -> None:
    """The hardening must not break the legitimate in-memory Arrow path."""
    engine = QueryEngine()
    try:
        engine.register("items", [{"qty": 2}, {"qty": 3}, {"qty": 5}])
        rows = engine.query("SELECT SUM(qty) AS total FROM items")
        assert rows == [{"total": 10}]
    finally:
        engine.close()


def test_external_file_read_is_denied(tmp_path) -> None:
    """A DuckDB-native local-file read must be rejected by the sandbox."""
    csv = tmp_path / "secret.csv"
    csv.write_text("name\nalice\n")

    engine = QueryEngine()
    try:
        with pytest.raises(duckdb.Error) as exc_info:
            engine.query(f"SELECT * FROM read_csv('{csv}')")
        # Surface the PermissionException specifically — a CatalogException
        # (function-not-found) would mean the read function was merely absent,
        # not that external access was actively denied.
        assert isinstance(exc_info.value, duckdb.PermissionException)
    finally:
        engine.close()


def test_configuration_is_locked() -> None:
    """``lock_configuration`` must prevent re-enabling external access."""
    engine = QueryEngine()
    try:
        with pytest.raises(duckdb.InvalidInputException):
            engine.conn.execute("SET enable_external_access=true")
    finally:
        engine.close()


def test_connection_uses_one_thread() -> None:
    """Each engine runs on one worker thread, and on duckdb 2.0+ one I/O thread.

    ``result_query`` opens a new engine for every query. duckdb 2.0 starts a
    pool of I/O threads per connection, sized by the CPU count, unless
    ``async_threads`` is set when the connection opens.
    """
    with QueryEngine() as engine:
        assert engine.query("SELECT current_setting('threads') AS t") == [{"t": 1}]
        if int(duckdb.__version__.split(".", 1)[0]) >= 2:
            rows = engine.query("SELECT current_setting('async_threads') AS t")
            assert rows == [{"t": 1}]


def test_registering_a_name_again_replaces_the_rows() -> None:
    """A second ``register`` under the same name replaces the table."""
    with QueryEngine() as engine:
        engine.register("items", [{"qty": 2}, {"qty": 3}])
        engine.register("items", [{"qty": 7}])
        assert engine.query("SELECT SUM(qty) AS total FROM items") == [{"total": 7}]
