"""Tests for connector CLI commands: add, list, test."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _setup_state_dir(tmp_path: Path) -> Path:
    """Create a minimal state dir with subdirectories and initialized DB."""
    state_dir = tmp_path / ".meho"
    state_dir.mkdir()
    for subdir in ["connectors", "credentials", "skills", "workflows", "logs", "db"]:
        (state_dir / subdir).mkdir()

    # Initialize databases
    from meho_claude.core.database import get_connection, run_migrations

    db_path = state_dir / "meho.db"
    conn = get_connection(db_path)
    run_migrations(conn, "meho_claude.db.migrations.meho")
    conn.close()

    return state_dir


class TestConnectorAdd:
    def test_add_non_interactive_creates_connector(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)
        spec_path = str(Path(__file__).parent.parent / "fixtures" / "petstore_openapi.yaml")

        mock_ops = [
            MagicMock(
                connector_name="petstore",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
                http_method="GET",
                url_template="/pets",
                description="List all pets",
                input_schema={},
                output_schema={},
                tags=["pets"],
                example_params={},
                related_operations=[],
            ),
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--name",
                    "petstore",
                    "--url",
                    "https://petstore.example.com",
                    "--spec",
                    spec_path,
                    "--auth-method",
                    "bearer",
                    "--credential-name",
                    "petstore-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "petstore"
        assert data["operations"] == 1
        assert "skill_file" in data

    def test_add_non_interactive_saves_yaml_config(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)
        spec_path = str(Path(__file__).parent.parent / "fixtures" / "petstore_openapi.yaml")

        mock_ops = [
            MagicMock(
                connector_name="petstore",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
                http_method="GET",
                url_template="/pets",
                description="",
                input_schema={},
                output_schema={},
                tags=[],
                example_params={},
                related_operations=[],
            ),
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--name",
                    "petstore",
                    "--url",
                    "https://petstore.example.com",
                    "--spec",
                    spec_path,
                    "--auth-method",
                    "bearer",
                    "--credential-name",
                    "petstore-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0
        yaml_path = state_dir / "connectors" / "petstore.yaml"
        assert yaml_path.exists()

    def test_add_non_interactive_generates_skill_file(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)
        spec_path = str(Path(__file__).parent.parent / "fixtures" / "petstore_openapi.yaml")

        mock_ops = [
            MagicMock(
                connector_name="petstore",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
                http_method="GET",
                url_template="/pets",
                description="",
                input_schema={},
                output_schema={},
                tags=["pets"],
                example_params={},
                related_operations=[],
            ),
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--name",
                    "petstore",
                    "--url",
                    "https://petstore.example.com",
                    "--spec",
                    spec_path,
                    "--auth-method",
                    "bearer",
                    "--credential-name",
                    "petstore-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0
        skill_path = state_dir / "skills" / "petstore.md"
        assert skill_path.exists()
        content = skill_path.read_text()
        assert "petstore" in content

    def test_add_cancellation(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)
        spec_path = str(Path(__file__).parent.parent / "fixtures" / "petstore_openapi.yaml")

        mock_ops = [
            MagicMock(
                connector_name="petstore",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
                tags=["pets"],
            ),
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._confirm_save", return_value=False),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--name",
                    "petstore",
                    "--url",
                    "https://petstore.example.com",
                    "--spec",
                    spec_path,
                    "--auth-method",
                    "bearer",
                    "--credential-name",
                    "petstore-creds",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "cancelled"

    def test_add_inserts_operations_into_db(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)
        spec_path = str(Path(__file__).parent.parent / "fixtures" / "petstore_openapi.yaml")

        mock_ops = [
            MagicMock(
                connector_name="petstore",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
                http_method="GET",
                url_template="/pets",
                description="List all pets",
                input_schema={"parameters": []},
                output_schema={},
                tags=["pets"],
                example_params={},
                related_operations=[],
            ),
            MagicMock(
                connector_name="petstore",
                operation_id="createPet",
                display_name="Create pet",
                trust_tier="WRITE",
                http_method="POST",
                url_template="/pets",
                description="Create a new pet",
                input_schema={"body": {}},
                output_schema={},
                tags=["pets"],
                example_params={},
                related_operations=[],
            ),
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--name",
                    "petstore",
                    "--url",
                    "https://petstore.example.com",
                    "--spec",
                    spec_path,
                    "--auth-method",
                    "bearer",
                    "--credential-name",
                    "petstore-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["operations"] == 2

        # Verify operations in DB
        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "meho.db")
        rows = conn.execute("SELECT * FROM operations WHERE connector_name = 'petstore'").fetchall()
        conn.close()
        assert len(rows) == 2


class TestConnectorAddSDK:
    """Tests for SDK connector wizard flows (kubernetes, vmware, proxmox, gcp)."""

    def _make_mock_ops(self, connector_name: str, connector_type: str) -> list:
        """Create mock operations for SDK connector tests."""
        return [
            MagicMock(
                connector_name=connector_name,
                operation_id=f"list-{connector_type}-resources",
                display_name=f"List {connector_type} resources",
                trust_tier="READ",
                http_method=None,
                url_template=None,
                description=f"List all {connector_type} resources",
                input_schema={},
                output_schema={},
                tags=[connector_type],
                example_params={},
                related_operations=[],
            ),
        ]

    def test_add_kubernetes_non_interactive(self, tmp_path):
        """Kubernetes add succeeds without prompting for OpenAPI spec."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("k8s-prod", "kubernetes")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops) as mock_sdk,
            patch("meho_claude.cli.connector._store_credentials") as mock_creds,
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "kubernetes",
                    "--name",
                    "k8s-prod",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "k8s-prod"
        assert data["connector_type"] == "kubernetes"
        # SDK discovery should have been called, NOT REST _discover_operations
        mock_sdk.assert_called_once()

    def test_add_kubernetes_skips_credential_storage(self, tmp_path):
        """Kubernetes add does NOT call _store_credentials (kubeconfig handles auth)."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("k8s-prod", "kubernetes")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials") as mock_creds,
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "kubernetes",
                    "--name",
                    "k8s-prod",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0
        mock_creds.assert_not_called()

    def test_add_vmware_non_interactive(self, tmp_path):
        """VMware add succeeds with URL and auth flags (no OpenAPI spec)."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("vcenter-prod", "vmware")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials") as mock_creds,
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "vmware",
                    "--name",
                    "vcenter-prod",
                    "--url",
                    "https://vcenter.local",
                    "--auth-method",
                    "basic",
                    "--credential-name",
                    "vc-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "vcenter-prod"
        assert data["connector_type"] == "vmware"

    def test_add_vmware_stores_credentials(self, tmp_path):
        """VMware add DOES call _store_credentials (basic auth needed)."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("vcenter-prod", "vmware")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials") as mock_creds,
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "vmware",
                    "--name",
                    "vcenter-prod",
                    "--url",
                    "https://vcenter.local",
                    "--auth-method",
                    "basic",
                    "--credential-name",
                    "vc-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0
        mock_creds.assert_called_once()

    def test_add_proxmox_non_interactive(self, tmp_path):
        """Proxmox add succeeds with URL and auth flags (no OpenAPI spec)."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("pve-prod", "proxmox")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "proxmox",
                    "--name",
                    "pve-prod",
                    "--url",
                    "https://pve.local:8006",
                    "--auth-method",
                    "basic",
                    "--credential-name",
                    "pve-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "pve-prod"
        assert data["connector_type"] == "proxmox"

    def test_add_gcp_non_interactive(self, tmp_path):
        """GCP add succeeds with project-id flag (no URL, no spec, no auth)."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("gcp-prod", "gcp")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials") as mock_creds,
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "gcp",
                    "--name",
                    "gcp-prod",
                    "--project-id",
                    "my-project",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "gcp-prod"
        assert data["connector_type"] == "gcp"
        # GCP uses ADC -- no credential storage
        mock_creds.assert_not_called()

    def test_add_rest_still_works(self, tmp_path):
        """REST add still works after refactoring (regression check)."""
        state_dir = _setup_state_dir(tmp_path)
        spec_path = str(Path(__file__).parent.parent / "fixtures" / "petstore_openapi.yaml")

        mock_ops = [
            MagicMock(
                connector_name="petstore",
                operation_id="listPets",
                display_name="List pets",
                trust_tier="READ",
                http_method="GET",
                url_template="/pets",
                description="List all pets",
                input_schema={},
                output_schema={},
                tags=["pets"],
                example_params={},
                related_operations=[],
            ),
        ]

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "rest",
                    "--name",
                    "petstore",
                    "--url",
                    "https://petstore.example.com",
                    "--spec",
                    spec_path,
                    "--auth-method",
                    "bearer",
                    "--credential-name",
                    "petstore-creds",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "petstore"
        assert data["connector_type"] == "rest"

    def test_add_kubernetes_saves_yaml_config(self, tmp_path):
        """YAML config saved to connectors dir with kubeconfig fields."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("k8s-test", "kubernetes")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "kubernetes",
                    "--name",
                    "k8s-test",
                    "--kubeconfig-path",
                    "/home/user/.kube/config",
                    "--kubeconfig-context",
                    "prod-cluster",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        yaml_path = state_dir / "connectors" / "k8s-test.yaml"
        assert yaml_path.exists()
        content = yaml_path.read_text()
        assert "kubernetes" in content
        assert "k8s-test" in content

    def test_add_sdk_generates_skill_file(self, tmp_path):
        """Skill file generated for SDK connector type."""
        state_dir = _setup_state_dir(tmp_path)
        mock_ops = self._make_mock_ops("k8s-skill", "kubernetes")

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._discover_sdk_operations", return_value=mock_ops),
            patch("meho_claude.cli.connector._store_credentials"),
        ):
            result = runner.invoke(
                app,
                [
                    "connector",
                    "add",
                    "--type",
                    "kubernetes",
                    "--name",
                    "k8s-skill",
                    "--non-interactive",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        skill_path = state_dir / "skills" / "k8s-skill.md"
        assert skill_path.exists()
        content = skill_path.read_text()
        assert "k8s-skill" in content


class TestConnectorList:
    def test_list_empty(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["connector", "list"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connectors"] == []
        assert data["count"] == 0

    def test_list_with_connectors(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        # Insert a connector directly into the DB
        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "meho.db")
        conn.execute(
            "INSERT INTO connectors (id, name, connector_type, base_url, config_path, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            ("uuid1", "petstore", "rest", "https://petstore.example.com", "/tmp/petstore.yaml", 1),
        )
        conn.commit()
        conn.close()

        with patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["connector", "list"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["count"] == 1
        assert data["connectors"][0]["name"] == "petstore"
        assert data["connectors"][0]["type"] == "rest"


class TestConnectorTest:
    def test_test_successful_connection(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        # Insert a connector and YAML config
        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "meho.db")
        conn.execute(
            "INSERT INTO connectors (id, name, connector_type, base_url, config_path, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            ("uuid1", "petstore", "rest", "https://petstore.example.com", "/tmp/petstore.yaml", 1),
        )
        conn.commit()
        conn.close()

        mock_result = {"status": "ok", "status_code": 200, "response_time_ms": 42}

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._test_connector_connection", return_value=mock_result),
        ):
            result = runner.invoke(app, ["connector", "test", "petstore"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["connector"] == "petstore"
        assert data["response_time_ms"] == 42

    def test_test_connection_failure(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        from meho_claude.core.database import get_connection

        conn = get_connection(state_dir / "meho.db")
        conn.execute(
            "INSERT INTO connectors (id, name, connector_type, base_url, config_path, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            ("uuid1", "petstore", "rest", "https://petstore.example.com", "/tmp/petstore.yaml", 1),
        )
        conn.commit()
        conn.close()

        mock_result = {"status": "error", "message": "Connection refused"}

        with (
            patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)),
            patch("meho_claude.cli.connector._test_connector_connection", return_value=mock_result),
        ):
            result = runner.invoke(app, ["connector", "test", "petstore"])

        # Should exit with error
        assert result.exit_code != 0 or "error" in result.output.lower()

    def test_test_unknown_connector(self, tmp_path):
        state_dir = _setup_state_dir(tmp_path)

        with patch("meho_claude.cli.connector._get_settings", return_value=_mock_settings(state_dir)):
            result = runner.invoke(app, ["connector", "test", "nonexistent"])

        assert result.exit_code != 0 or "error" in result.output.lower()
