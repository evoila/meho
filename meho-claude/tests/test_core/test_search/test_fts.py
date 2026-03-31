"""Tests for FTS5 BM25 search over operations."""

import json
import sqlite3

import pytest

from meho_claude.core.search.fts import sanitize_fts_query, search_bm25


@pytest.fixture
def fts_db(tmp_path):
    """Create a SQLite DB with operations table and FTS5 index populated with test data."""
    db_path = tmp_path / "meho.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Create operations table
    conn.executescript("""
        CREATE TABLE operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connector_name TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            trust_tier TEXT NOT NULL DEFAULT 'READ',
            http_method TEXT,
            url_template TEXT,
            input_schema TEXT DEFAULT '{}',
            output_schema TEXT DEFAULT '{}',
            tags TEXT DEFAULT '',
            example_params TEXT DEFAULT '{}',
            related_operations TEXT DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(connector_name, operation_id)
        );

        CREATE VIRTUAL TABLE operations_fts USING fts5(
            operation_id,
            display_name,
            description,
            tags,
            content='operations',
            content_rowid='id',
            tokenize='porter unicode61'
        );

        CREATE TRIGGER operations_ai AFTER INSERT ON operations BEGIN
            INSERT INTO operations_fts(rowid, operation_id, display_name, description, tags)
            VALUES (new.id, new.operation_id, new.display_name, new.description, new.tags);
        END;

        CREATE TRIGGER operations_ad AFTER DELETE ON operations BEGIN
            INSERT INTO operations_fts(operations_fts, rowid, operation_id, display_name, description, tags)
            VALUES ('delete', old.id, old.operation_id, old.display_name, old.description, old.tags);
        END;

        CREATE TRIGGER operations_au AFTER UPDATE ON operations BEGIN
            INSERT INTO operations_fts(operations_fts, rowid, operation_id, display_name, description, tags)
            VALUES ('delete', old.id, old.operation_id, old.display_name, old.description, old.tags);
            INSERT INTO operations_fts(rowid, operation_id, display_name, description, tags)
            VALUES (new.id, new.operation_id, new.display_name, new.description, new.tags);
        END;
    """)

    # Insert test operations
    test_ops = [
        ("k8s-prod", "listPods", "List Pods", "List all running pods in the cluster", "READ", "GET", "/api/v1/pods", "kubernetes,pods"),
        ("k8s-prod", "deletePod", "Delete Pod", "Delete a specific pod by name", "DESTRUCTIVE", "DELETE", "/api/v1/pods/{name}", "kubernetes,pods"),
        ("k8s-prod", "createDeployment", "Create Deployment", "Create a new deployment in the cluster", "WRITE", "POST", "/apis/apps/v1/deployments", "kubernetes,deployments"),
        ("vmware-dc", "listVMs", "List Virtual Machines", "List all VMs in vCenter", "READ", "GET", "/rest/vcenter/vm", "vmware,vms"),
        ("vmware-dc", "powerOnVM", "Power On VM", "Power on a virtual machine", "WRITE", "POST", "/rest/vcenter/vm/{vm}/power/start", "vmware,vms,power"),
    ]

    for op in test_ops:
        conn.execute(
            """INSERT INTO operations
               (connector_name, operation_id, display_name, description, trust_tier, http_method, url_template, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            op,
        )
    conn.commit()

    yield conn
    conn.close()


class TestSanitizeFtsQuery:
    def test_strips_and_operator(self):
        assert "AND" not in sanitize_fts_query("list AND pods")

    def test_strips_or_operator(self):
        assert "OR" not in sanitize_fts_query("list OR pods")

    def test_strips_not_operator(self):
        assert "NOT" not in sanitize_fts_query("NOT pods")

    def test_strips_near_operator(self):
        assert "NEAR" not in sanitize_fts_query("list NEAR pods")

    def test_strips_colons(self):
        result = sanitize_fts_query("display_name:pods")
        assert ":" not in result

    def test_strips_quotes(self):
        result = sanitize_fts_query('"list pods"')
        # Should re-wrap individual tokens
        assert result  # non-empty

    def test_strips_parens(self):
        result = sanitize_fts_query("(list OR pods)")
        assert "(" not in result
        assert ")" not in result

    def test_strips_asterisks(self):
        result = sanitize_fts_query("list*")
        assert "*" not in result

    def test_wraps_tokens_in_double_quotes(self):
        result = sanitize_fts_query("list pods")
        assert '"list"' in result
        assert '"pods"' in result

    def test_empty_input_returns_empty(self):
        assert sanitize_fts_query("") == ""

    def test_only_operators_returns_empty(self):
        assert sanitize_fts_query("AND OR NOT") == ""

    def test_whitespace_only_returns_empty(self):
        assert sanitize_fts_query("   ") == ""


class TestSearchBm25:
    def test_returns_results_for_matching_query(self, fts_db):
        results = search_bm25(fts_db, "list pods")
        assert len(results) > 0
        # listPods should be the top result (matches both display_name and description)
        assert results[0]["operation_id"] == "listPods"

    def test_returns_list_of_dicts(self, fts_db):
        results = search_bm25(fts_db, "pods")
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, dict)
            assert "id" in r
            assert "connector_name" in r
            assert "operation_id" in r
            assert "display_name" in r
            assert "description" in r
            assert "trust_tier" in r
            assert "bm25_score" in r

    def test_empty_query_returns_empty(self, fts_db):
        results = search_bm25(fts_db, "")
        assert results == []

    def test_no_match_returns_empty(self, fts_db):
        results = search_bm25(fts_db, "xyznonexistent")
        assert results == []

    def test_respects_limit(self, fts_db):
        results = search_bm25(fts_db, "list", limit=1)
        assert len(results) <= 1

    def test_connector_filter_works(self, fts_db):
        results = search_bm25(fts_db, "list", connector_name="vmware-dc")
        for r in results:
            assert r["connector_name"] == "vmware-dc"

    def test_sanitizes_query_before_match(self, fts_db):
        # Should not crash on FTS5 operators
        results = search_bm25(fts_db, "list AND pods OR NOT vm*")
        assert isinstance(results, list)
