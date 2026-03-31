"""Tests for topology CLI commands: lookup, correlate."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from meho_claude.cli import app

runner = CliRunner()


def _mock_settings(tmp_state_dir: Path):
    """Create a mock MehoSettings pointing to tmp state dir."""
    settings = MagicMock()
    settings.state_dir = tmp_state_dir
    settings.debug = False
    return settings


def _setup_topology_state(tmp_path: Path) -> Path:
    """Create a minimal state dir with topology.db initialized."""
    state_dir = tmp_path / ".meho"
    state_dir.mkdir()
    for subdir in ["connectors", "credentials", "skills", "workflows", "logs", "db"]:
        (state_dir / subdir).mkdir()

    from meho_claude.core.database import get_connection, run_migrations

    # Initialize both DBs (main callback needs meho.db too)
    meho_conn = get_connection(state_dir / "meho.db")
    run_migrations(meho_conn, "meho_claude.db.migrations.meho")
    meho_conn.close()

    topo_conn = get_connection(state_dir / "topology.db")
    run_migrations(topo_conn, "meho_claude.db.migrations.topology")
    topo_conn.close()

    return state_dir


def _insert_entity(conn, entity_id, name, entity_type, connector_name, connector_type,
                   canonical_id, description="", scope_json="{}", raw_attributes_json="{}",
                   connector_id=None):
    """Insert a topology entity directly into the DB."""
    conn.execute(
        """INSERT INTO topology_entities
           (id, name, connector_id, connector_name, entity_type, connector_type,
            scope_json, canonical_id, description, raw_attributes_json, embedding_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (entity_id, name, connector_id or "conn-1", connector_name, entity_type,
         connector_type, scope_json, canonical_id, description, raw_attributes_json, "hash1"),
    )
    conn.commit()


def _insert_relationship(conn, rel_id, from_id, to_id, rel_type):
    """Insert a topology relationship directly into the DB."""
    conn.execute(
        """INSERT INTO topology_relationships
           (id, from_entity_id, to_entity_id, relationship_type)
           VALUES (?, ?, ?, ?)""",
        (rel_id, from_id, to_id, rel_type),
    )
    conn.commit()


def _insert_correlation(conn, corr_id, entity_a_id, entity_b_id, match_type,
                        confidence, match_details, status="pending"):
    """Insert a topology correlation directly into the DB."""
    conn.execute(
        """INSERT INTO topology_correlations
           (id, entity_a_id, entity_b_id, match_type, confidence, match_details, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (corr_id, entity_a_id, entity_b_id, match_type, confidence,
         json.dumps(match_details), status),
    )
    conn.commit()


# ---- Lookup Tests ----


class TestTopologyLookup:
    def test_lookup_exact_id_returns_entity(self, tmp_path):
        """Exact UUID match returns full entity details."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-uuid-1", "nginx-pod", "Pod", "k8s-prod", "kubernetes",
                       "default/nginx", description="NGINX web server")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "lookup", "ent-uuid-1"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["entity"]["name"] == "nginx-pod"
        assert data["entity"]["entity_type"] == "Pod"
        assert "relationships" in data
        assert "correlations" in data

    def test_lookup_fuzzy_single_match(self, tmp_path):
        """Fuzzy search with a single match returns full entity details."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-uuid-1", "nginx-pod", "Pod", "k8s-prod", "kubernetes",
                       "default/nginx", description="NGINX web server")
        conn.close()

        single_result = [{
            "id": "ent-uuid-1", "name": "nginx-pod", "entity_type": "Pod",
            "connector_name": "k8s-prod", "relevance_score": 0.5,
        }]

        with (
            patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.topology._topology_search", return_value=single_result),
        ):
            result = runner.invoke(app, ["topology", "lookup", "nginx"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["entity"]["name"] == "nginx-pod"

    def test_lookup_fuzzy_multiple_matches(self, tmp_path):
        """Fuzzy search with multiple matches returns candidates list."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-uuid-1", "nginx-pod-1", "Pod", "k8s-prod", "kubernetes",
                       "default/nginx-1")
        _insert_entity(conn, "ent-uuid-2", "nginx-pod-2", "Pod", "k8s-prod", "kubernetes",
                       "default/nginx-2")
        conn.close()

        multi_results = [
            {"id": "ent-uuid-1", "name": "nginx-pod-1", "entity_type": "Pod",
             "connector_name": "k8s-prod", "relevance_score": 0.5},
            {"id": "ent-uuid-2", "name": "nginx-pod-2", "entity_type": "Pod",
             "connector_name": "k8s-prod", "relevance_score": 0.4},
        ]

        with (
            patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.topology._topology_search", return_value=multi_results),
        ):
            result = runner.invoke(app, ["topology", "lookup", "nginx"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "multiple_matches"
        assert len(data["candidates"]) == 2
        # Each candidate has minimal info for Claude to disambiguate
        candidate = data["candidates"][0]
        assert "id" in candidate
        assert "name" in candidate
        assert "entity_type" in candidate

    def test_lookup_no_match_returns_error(self, tmp_path):
        """No match returns error with ENTITY_NOT_FOUND code."""
        state_dir = _setup_topology_state(tmp_path)

        with (
            patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.topology._topology_search", return_value=[]),
        ):
            result = runner.invoke(app, ["topology", "lookup", "nonexistent"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "ENTITY_NOT_FOUND"

    def test_lookup_depth_triggers_deeper_traversal(self, tmp_path):
        """--depth 2 includes 2-hop relationships."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-1", "pod-a", "Pod", "k8s-prod", "kubernetes", "ns/pod-a")
        _insert_entity(conn, "ent-2", "node-b", "Node", "k8s-prod", "kubernetes", "node-b")
        _insert_entity(conn, "ent-3", "cluster-c", "Cluster", "k8s-prod", "kubernetes", "cluster-c")
        _insert_relationship(conn, "rel-1", "ent-1", "ent-2", "runs_on")
        _insert_relationship(conn, "rel-2", "ent-2", "ent-3", "member_of")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "lookup", "ent-1", "--depth", "2"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        # Should include both depth-1 (node-b) and depth-2 (cluster-c)
        rel_names = [r["name"] for r in data["relationships"]]
        assert "node-b" in rel_names
        assert "cluster-c" in rel_names

    def test_lookup_connector_filter(self, tmp_path):
        """--connector flag filters search to that connector."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-uuid-1", "nginx-pod", "Pod", "k8s-prod", "kubernetes",
                       "default/nginx")
        conn.close()

        filtered_results = [{
            "id": "ent-uuid-1", "name": "nginx-pod", "entity_type": "Pod",
            "connector_name": "k8s-prod", "relevance_score": 0.5,
        }]

        with (
            patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.topology._topology_search", return_value=filtered_results) as mock_search,
        ):
            result = runner.invoke(app, ["topology", "lookup", "nginx", "--connector", "k8s-prod"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Verify connector filter was passed to search
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args
        assert call_kwargs[1].get("connector_name") == "k8s-prod" or call_kwargs[0][3] == "k8s-prod"

    def test_lookup_json_output_structure(self, tmp_path):
        """JSON output has correct keys: entity, relationships, correlations, status."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-uuid-1", "nginx-pod", "Pod", "k8s-prod", "kubernetes",
                       "default/nginx", description="Web server")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "lookup", "ent-uuid-1"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "status" in data
        assert "entity" in data
        assert "relationships" in data
        assert "correlations" in data
        assert "duration_ms" in data

    def test_lookup_includes_correlations(self, tmp_path):
        """Lookup includes pending correlations for the entity."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-1", "nginx-k8s", "Pod", "k8s-prod", "kubernetes", "ns/nginx")
        _insert_entity(conn, "ent-2", "nginx-vm", "VM", "vmware-prod", "vmware", "nginx-vm",
                       raw_attributes_json='{"ip_address": "10.0.0.1"}')
        _insert_correlation(conn, "corr-1", "ent-1", "ent-2", "ip_match", 0.8,
                           {"match_field": "ip_address"}, status="pending")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "lookup", "ent-1"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["correlations"]) == 1
        assert data["correlations"][0]["match_type"] == "ip_match"


# ---- Correlate Tests ----


class TestTopologyCorrelate:
    def test_correlate_no_pending_returns_empty(self, tmp_path):
        """correlate with no pending correlations returns empty list."""
        state_dir = _setup_topology_state(tmp_path)

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["correlations"] == []
        assert data["pending_count"] == 0

    def test_correlate_with_pending_returns_them(self, tmp_path):
        """correlate returns pending correlations."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-1", "nginx-k8s", "Pod", "k8s-prod", "kubernetes", "ns/nginx")
        _insert_entity(conn, "ent-2", "nginx-vm", "VM", "vmware-prod", "vmware", "nginx-vm")
        _insert_correlation(conn, "corr-1", "ent-1", "ent-2", "ip_match", 0.8,
                           {"match_field": "ip_address"}, status="pending")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["correlations"]) == 1
        assert data["pending_count"] == 1
        corr = data["correlations"][0]
        assert corr["id"] == "corr-1"
        assert corr["match_type"] == "ip_match"
        assert corr["entity_a_name"] == "nginx-k8s"
        assert corr["entity_b_name"] == "nginx-vm"

    def test_correlate_confirm_updates_status(self, tmp_path):
        """correlate --confirm updates status to confirmed."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-1", "nginx-k8s", "Pod", "k8s-prod", "kubernetes", "ns/nginx")
        _insert_entity(conn, "ent-2", "nginx-vm", "VM", "vmware-prod", "vmware", "nginx-vm")
        _insert_correlation(conn, "corr-1", "ent-1", "ent-2", "ip_match", 0.8,
                           {"match_field": "ip_address"}, status="pending")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate", "--confirm", "corr-1"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["action"] == "confirmed"
        assert data["correlation"]["status"] == "confirmed"

    def test_correlate_reject_updates_status(self, tmp_path):
        """correlate --reject updates status to rejected."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-1", "nginx-k8s", "Pod", "k8s-prod", "kubernetes", "ns/nginx")
        _insert_entity(conn, "ent-2", "nginx-vm", "VM", "vmware-prod", "vmware", "nginx-vm")
        _insert_correlation(conn, "corr-1", "ent-1", "ent-2", "ip_match", 0.8,
                           {"match_field": "ip_address"}, status="pending")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate", "--reject", "corr-1"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["action"] == "rejected"
        assert data["correlation"]["status"] == "rejected"

    def test_correlate_confirm_invalid_id_returns_error(self, tmp_path):
        """correlate --confirm with invalid ID returns error."""
        state_dir = _setup_topology_state(tmp_path)

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate", "--confirm", "invalid-id"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["code"] == "CORRELATION_NOT_FOUND"

    def test_correlate_all_shows_all_statuses(self, tmp_path):
        """correlate --all shows confirmed, rejected, and pending."""
        state_dir = _setup_topology_state(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "topology.db")
        _insert_entity(conn, "ent-1", "nginx-k8s", "Pod", "k8s-prod", "kubernetes", "ns/nginx")
        _insert_entity(conn, "ent-2", "nginx-vm", "VM", "vmware-prod", "vmware", "nginx-vm")
        _insert_entity(conn, "ent-3", "nginx-gcp", "VM", "gcp-prod", "gcp", "nginx-gcp")
        _insert_correlation(conn, "corr-1", "ent-1", "ent-2", "ip_match", 0.8,
                           {"match_field": "ip_address"}, status="pending")
        _insert_correlation(conn, "corr-2", "ent-1", "ent-3", "hostname_match", 0.7,
                           {"match_field": "hostname"}, status="confirmed")
        conn.close()

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate", "--all"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert len(data["correlations"]) == 2
        statuses = {c["status"] for c in data["correlations"]}
        assert "pending" in statuses
        assert "confirmed" in statuses

    def test_correlate_json_output_structure(self, tmp_path):
        """JSON output has correct keys: status, correlations, pending_count."""
        state_dir = _setup_topology_state(tmp_path)

        with patch("meho_claude.cli.topology._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["topology", "correlate"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "status" in data
        assert "correlations" in data
        assert "pending_count" in data
        assert "duration_ms" in data
