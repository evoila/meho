"""Tests for CorrelationEngine deterministic matching."""

import json
import uuid

import pytest

from meho_claude.core.topology.models import TopologyEntity


def _make_entity(
    name="nginx",
    entity_type="Pod",
    connector_type="kubernetes",
    connector_id="k8s-prod",
    connector_name="prod-cluster",
    canonical_id="default/nginx",
    description="",
    raw_attributes=None,
    scope=None,
):
    """Helper to create a TopologyEntity."""
    return TopologyEntity(
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=connector_id,
        connector_name=connector_name,
        canonical_id=canonical_id,
        description=description,
        raw_attributes=raw_attributes or {},
        scope=scope or {},
    )


def _insert_entity(conn, entity):
    """Insert an entity directly into topology.db for correlation testing."""
    conn.execute(
        """INSERT INTO topology_entities
           (id, name, connector_id, connector_name, entity_type, connector_type,
            scope_json, canonical_id, description, raw_attributes_json, embedding_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (entity.id, entity.name, entity.connector_id, entity.connector_name,
         entity.entity_type, entity.connector_type, json.dumps(entity.scope),
         entity.canonical_id, entity.description, json.dumps(entity.raw_attributes), ""),
    )
    conn.commit()


class TestProviderIdCorrelation:
    """Provider ID matching: auto-confirm at confidence 1.0."""

    def test_provider_id_auto_confirms(self, topology_db):
        """Provider ID match should create confirmed correlation at 1.0."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        # Entity from VMware
        vm = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc-prod", connector_name="vcenter",
            canonical_id="dc1/vm-1",
            raw_attributes={"provider_id": "vm-12345"},
        )
        _insert_entity(topology_db, vm)

        # Entity from K8s with same provider_id
        node = _make_entity(
            name="node-1", entity_type="Node", connector_type="kubernetes",
            connector_id="k8s-prod", connector_name="prod-cluster",
            canonical_id="node-1",
            raw_attributes={"provider_id": "vm-12345"},
        )
        _insert_entity(topology_db, node)

        correlations = engine.correlate_entity(node)
        assert len(correlations) == 1
        assert correlations[0]["status"] == "confirmed"

        # Check DB
        row = topology_db.execute(
            "SELECT * FROM topology_correlations WHERE status = 'confirmed'"
        ).fetchone()
        assert row is not None
        assert row["confidence"] == 1.0

    def test_provider_id_stores_evidence(self, topology_db):
        """Provider ID correlation should store match evidence."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        vm = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="dc1/vm-1",
            raw_attributes={"provider_id": "prov-abc"},
        )
        _insert_entity(topology_db, vm)

        node = _make_entity(
            name="node-1", entity_type="Node",
            connector_id="k8s", connector_name="k8s-cluster", canonical_id="node-1",
            raw_attributes={"provider_id": "prov-abc"},
        )
        _insert_entity(topology_db, node)

        engine.correlate_entity(node)

        row = topology_db.execute(
            "SELECT match_details FROM topology_correlations"
        ).fetchone()
        evidence = json.loads(row["match_details"])
        assert evidence["match_field"] == "provider_id"
        assert evidence["entity_a_value"] == "prov-abc"
        assert evidence["entity_b_value"] == "prov-abc"
        assert evidence["match_type"] == "exact"


class TestIpCorrelation:
    """IP address matching: pending at confidence 0.8."""

    def test_ip_match_creates_pending(self, topology_db):
        """IP address match should create pending correlation at 0.8."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        vm = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="dc1/vm-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, vm)

        pod = _make_entity(
            name="pod-1", entity_type="Pod",
            connector_id="k8s", connector_name="k8s-cluster",
            canonical_id="default/pod-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, pod)

        correlations = engine.correlate_entity(pod)
        assert len(correlations) == 1
        assert correlations[0]["status"] == "pending"

        row = topology_db.execute(
            "SELECT * FROM topology_correlations WHERE status = 'pending'"
        ).fetchone()
        assert row is not None
        assert row["confidence"] == 0.8


class TestHostnameCorrelation:
    """Hostname matching: pending at confidence 0.7."""

    def test_hostname_match_creates_pending(self, topology_db):
        """Hostname match should create pending correlation at 0.7."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        vm = _make_entity(
            name="server-1", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="dc1/server-1",
            raw_attributes={"hostname": "server-1.prod.local"},
        )
        _insert_entity(topology_db, vm)

        node = _make_entity(
            name="node-1", entity_type="Node",
            connector_id="k8s", connector_name="k8s-cluster",
            canonical_id="node-1",
            raw_attributes={"hostname": "server-1.prod.local"},
        )
        _insert_entity(topology_db, node)

        correlations = engine.correlate_entity(node)
        assert len(correlations) == 1
        assert correlations[0]["status"] == "pending"

        row = topology_db.execute(
            "SELECT * FROM topology_correlations WHERE status = 'pending'"
        ).fetchone()
        assert row["confidence"] == 0.7


class TestCorrelationRules:
    """Correlation engine rules: same-connector skip, duplicate prevention."""

    def test_same_connector_never_correlated(self, topology_db):
        """Entities from the same connector should never be correlated."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        # Two entities from same connector with matching IP
        pod1 = _make_entity(
            name="pod-1", canonical_id="default/pod-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, pod1)

        pod2 = _make_entity(
            name="pod-2", canonical_id="default/pod-2",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, pod2)

        correlations = engine.correlate_entity(pod2)
        assert len(correlations) == 0

    def test_duplicate_correlation_prevented(self, topology_db):
        """Should not create duplicate correlations (bidirectional check)."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        vm = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="dc1/vm-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, vm)

        pod = _make_entity(
            name="pod-1", canonical_id="default/pod-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, pod)

        # First correlation
        correlations1 = engine.correlate_entity(pod)
        assert len(correlations1) == 1

        # Second correlation attempt -- should be skipped
        correlations2 = engine.correlate_entity(pod)
        assert len(correlations2) == 0

        # Only one in DB
        count = topology_db.execute(
            "SELECT COUNT(*) as c FROM topology_correlations"
        ).fetchone()["c"]
        assert count == 1

    def test_duplicate_bidirectional(self, topology_db):
        """Correlation A->B should prevent B->A."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        vm = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="dc1/vm-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, vm)

        pod = _make_entity(
            name="pod-1", canonical_id="default/pod-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, pod)

        # Correlate from pod side
        engine.correlate_entity(pod)

        # Now correlate from vm side -- should not create duplicate
        correlations = engine.correlate_entity(vm)
        assert len(correlations) == 0

    def test_no_correlation_without_matching_attributes(self, topology_db):
        """Entities without matching attributes should not be correlated."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        vm = _make_entity(
            name="vm-1", entity_type="VM", connector_type="vmware",
            connector_id="vc", connector_name="vcenter", canonical_id="dc1/vm-1",
            raw_attributes={"ip_address": "10.0.1.5"},
        )
        _insert_entity(topology_db, vm)

        pod = _make_entity(
            name="pod-1", canonical_id="default/pod-1",
            raw_attributes={"ip_address": "10.0.2.99"},
        )
        _insert_entity(topology_db, pod)

        correlations = engine.correlate_entity(pod)
        assert len(correlations) == 0


class TestConfirmReject:
    """CorrelationEngine.confirm_correlation and reject_correlation."""

    def test_confirm_correlation(self, topology_db):
        """Confirming a correlation sets status=confirmed and resolved fields."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        # Create a pending correlation
        corr_id = str(uuid.uuid4())
        e1_id = str(uuid.uuid4())
        e2_id = str(uuid.uuid4())

        # Insert dummy entities
        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id) VALUES (?, ?, ?, ?, ?)""",
            (e1_id, "e1", "Pod", "kubernetes", "e1"),
        )
        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id) VALUES (?, ?, ?, ?, ?)""",
            (e2_id, "e2", "VM", "vmware", "e2"),
        )
        topology_db.execute(
            """INSERT INTO topology_correlations
               (id, entity_a_id, entity_b_id, match_type, confidence, match_details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (corr_id, e1_id, e2_id, "ip_match", 0.8, "{}", "pending"),
        )
        topology_db.commit()

        engine.confirm_correlation(corr_id)

        row = topology_db.execute(
            "SELECT status, resolved_by FROM topology_correlations WHERE id = ?",
            (corr_id,),
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["resolved_by"] == "user"

    def test_reject_correlation(self, topology_db):
        """Rejecting a correlation sets status=rejected and resolved fields."""
        from meho_claude.core.topology.correlator import CorrelationEngine

        engine = CorrelationEngine(topology_db)

        corr_id = str(uuid.uuid4())
        e1_id = str(uuid.uuid4())
        e2_id = str(uuid.uuid4())

        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id) VALUES (?, ?, ?, ?, ?)""",
            (e1_id, "e1", "Pod", "kubernetes", "e1"),
        )
        topology_db.execute(
            """INSERT INTO topology_entities
               (id, name, entity_type, connector_type, canonical_id) VALUES (?, ?, ?, ?, ?)""",
            (e2_id, "e2", "VM", "vmware", "e2"),
        )
        topology_db.execute(
            """INSERT INTO topology_correlations
               (id, entity_a_id, entity_b_id, match_type, confidence, match_details, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (corr_id, e1_id, e2_id, "ip_match", 0.8, "{}", "pending"),
        )
        topology_db.commit()

        engine.reject_correlation(corr_id)

        row = topology_db.execute(
            "SELECT status, resolved_by FROM topology_correlations WHERE id = ?",
            (corr_id,),
        ).fetchone()
        assert row["status"] == "rejected"
        assert row["resolved_by"] == "user"
