"""CorrelationEngine: deterministic cross-system entity correlation.

Matches entities from DIFFERENT connectors using provider_id, IP address,
and hostname attributes. Provider ID matches auto-confirm at confidence 1.0;
IP and hostname matches remain pending for user review.
"""

import json
import sqlite3
import uuid

import structlog

from meho_claude.core.topology.models import TopologyEntity

logger = structlog.get_logger()


class CorrelationEngine:
    """Deterministic cross-system entity correlation.

    Runs eagerly on every entity insert to keep correlations up-to-date.
    """

    # Matchable attributes extracted from raw_attributes
    MATCH_FIELDS: dict[str, dict] = {
        "provider_id": {"confidence": 1.0, "auto_confirm": True},
        "ip_address": {"confidence": 0.8, "auto_confirm": False},
        "hostname": {"confidence": 0.7, "auto_confirm": False},
    }

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def correlate_entity(self, entity: TopologyEntity) -> list[dict]:
        """Find and store correlations for a newly upserted entity.

        Checks each match field (provider_id, ip_address, hostname) against
        entities from OTHER connectors. Creates correlations with appropriate
        confidence and auto-confirm behavior.
        """
        attrs = entity.raw_attributes or {}
        correlations: list[dict] = []

        for field, config in self.MATCH_FIELDS.items():
            value = attrs.get(field)
            if not value:
                continue

            # Find candidate entities from other connectors with matching attribute
            # Use SQL LIKE for fast candidate filtering, then Python exact match
            candidates = self.conn.execute(
                """SELECT id, name, entity_type, connector_name, raw_attributes_json
                   FROM topology_entities
                   WHERE connector_name != ? AND id != ?
                   AND raw_attributes_json LIKE ?""",
                (entity.connector_name, entity.id, f'%"{field}"%'),
            ).fetchall()

            for candidate in candidates:
                candidate_attrs = json.loads(candidate["raw_attributes_json"] or "{}")
                if candidate_attrs.get(field) != value:
                    continue  # LIKE matched but exact value differs

                correlation = self._create_correlation(
                    entity_a_id=entity.id,
                    entity_b_id=candidate["id"],
                    match_type=f"{field}_match",
                    confidence=config["confidence"],
                    auto_confirm=config["auto_confirm"],
                    evidence={
                        "match_field": field,
                        "entity_a_value": str(value),
                        "entity_b_value": str(candidate_attrs[field]),
                        "match_type": "exact",
                    },
                )
                if correlation:
                    correlations.append(correlation)

        return correlations

    def _create_correlation(
        self,
        entity_a_id: str,
        entity_b_id: str,
        match_type: str,
        confidence: float,
        auto_confirm: bool,
        evidence: dict,
    ) -> dict | None:
        """Insert correlation if not already exists. Returns correlation dict or None.

        Checks both directions (A->B and B->A) to prevent duplicates.
        """
        # Check for existing correlation (either direction)
        existing = self.conn.execute(
            """SELECT id FROM topology_correlations
               WHERE (entity_a_id = ? AND entity_b_id = ?)
                  OR (entity_a_id = ? AND entity_b_id = ?)""",
            (entity_a_id, entity_b_id, entity_b_id, entity_a_id),
        ).fetchone()

        if existing:
            return None

        status = "confirmed" if auto_confirm else "pending"
        corr_id = str(uuid.uuid4())

        if auto_confirm:
            self.conn.execute(
                """INSERT INTO topology_correlations
                   (id, entity_a_id, entity_b_id, match_type, confidence,
                    match_details, status, resolved_at, resolved_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
                (
                    corr_id,
                    entity_a_id,
                    entity_b_id,
                    match_type,
                    confidence,
                    json.dumps(evidence),
                    status,
                    "auto",
                ),
            )
        else:
            self.conn.execute(
                """INSERT INTO topology_correlations
                   (id, entity_a_id, entity_b_id, match_type, confidence,
                    match_details, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    corr_id,
                    entity_a_id,
                    entity_b_id,
                    match_type,
                    confidence,
                    json.dumps(evidence),
                    status,
                ),
            )

        self.conn.commit()
        logger.debug(
            "correlation_created",
            correlation_id=corr_id,
            match_type=match_type,
            confidence=confidence,
            status=status,
        )

        return {"id": corr_id, "status": status, "match_type": match_type}

    def confirm_correlation(self, correlation_id: str) -> None:
        """Confirm a pending correlation. Sets status=confirmed, resolved_by=user."""
        self.conn.execute(
            """UPDATE topology_correlations
               SET status = 'confirmed', resolved_at = datetime('now'), resolved_by = 'user'
               WHERE id = ?""",
            (correlation_id,),
        )
        self.conn.commit()

    def reject_correlation(self, correlation_id: str) -> None:
        """Reject a pending correlation. Sets status=rejected, resolved_by=user."""
        self.conn.execute(
            """UPDATE topology_correlations
               SET status = 'rejected', resolved_at = datetime('now'), resolved_by = 'user'
               WHERE id = ?""",
            (correlation_id,),
        )
        self.conn.commit()
