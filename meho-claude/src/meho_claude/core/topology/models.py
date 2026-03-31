"""Pydantic v2 models for topology entities, relationships, and correlations.

Mirrors the topology.db schema (001_initial.sql + 002_fts5.sql).
"""

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel, Field


def compute_embedding_hash(entity_type: str, name: str, description: str) -> str:
    """Compute SHA-256 hash of embedding-relevant fields.

    Used to detect changes that require re-embedding in ChromaDB.
    Only entity_type, name, and description affect embeddings --
    changes to raw_attributes, last_verified_at, etc. do not trigger re-embedding.
    """
    canonical = f"{entity_type}:{name}:{description}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TopologyEntity(BaseModel):
    """A discovered infrastructure entity stored in topology.db.

    Entities are auto-discovered from connector query results.
    The unique identity is (connector_id, entity_type, canonical_id).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    connector_id: str | None = None
    connector_name: str | None = None
    entity_type: str
    connector_type: str
    scope: dict = Field(default_factory=dict)
    canonical_id: str
    description: str = ""
    raw_attributes: dict = Field(default_factory=dict)
    embedding_hash: str = ""


class TopologyRelationship(BaseModel):
    """A relationship between two topology entities.

    Valid relationship types: runs_on, routes_to, uses_storage, member_of,
    contains, connects_to.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_entity_id: str
    to_entity_id: str
    relationship_type: Literal[
        "runs_on", "routes_to", "uses_storage", "member_of", "contains", "connects_to"
    ]


class TopologyCorrelation(BaseModel):
    """A cross-system entity correlation (SAME_AS match).

    Provider ID matches auto-confirm at confidence 1.0.
    IP/hostname matches remain pending for user review.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    entity_a_id: str
    entity_b_id: str
    match_type: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_details: dict = Field(default_factory=dict)
    status: Literal["pending", "confirmed", "rejected"] = "pending"


class ExtractionResult(BaseModel):
    """Result of extracting topology from a connector operation.

    Contains both entities and relationships discovered in a single pass.
    """

    entities: list[TopologyEntity]
    relationships: list[TopologyRelationship]
    source_connector: str
    source_operation: str
