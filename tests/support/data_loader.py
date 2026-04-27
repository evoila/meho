# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Load test fixtures from JSON files into database.

Usage:
    from tests.support.data_loader import load_fixture

    await load_fixture("connectors", session)
"""

import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class FixtureLoader:
    """Load test fixtures into database"""

    def __init__(self, fixtures_dir: Path | None = None):
        if fixtures_dir is None:
            # Default to tests/fixtures/
            fixtures_dir = Path(__file__).parent.parent / "fixtures"
        self.fixtures_dir = Path(fixtures_dir)

    def load_json(self, fixture_name: str) -> list[dict[str, Any]]:
        """
        Load fixture data from JSON file.

        Args:
            fixture_name: Name of fixture file (without .json extension)

        Returns:
            List of fixture data dicts

        Raises:
            FileNotFoundError: If fixture file doesn't exist
        """
        fixture_path = self.fixtures_dir / f"{fixture_name}.json"

        if not fixture_path.exists():
            raise FileNotFoundError(f"Fixture file not found: {fixture_path}")

        with open(fixture_path) as f:
            data = json.load(f)

        return data if isinstance(data, list) else [data]

    async def load_fixture(
        self, fixture_name: str, session: AsyncSession, model_class: Any
    ) -> list[Any]:
        """
        Load a fixture file and create model instances in database.

        Args:
            fixture_name: Name of fixture file (without .json)
            session: Database session
            model_class: SQLAlchemy model class

        Returns:
            List of created model instances
        """
        data_list = self.load_json(fixture_name)

        instances = []
        for item_data in data_list:
            instance = model_class(**item_data)
            session.add(instance)
            instances.append(instance)

        await session.flush()
        return instances

    async def load_all_fixtures(self, session: AsyncSession, model_mapping: dict[str, Any]):
        """
        Load all fixtures based on model mapping.

        Args:
            session: Database session
            model_mapping: Dict of fixture_name -> model_class

        Example:
            await loader.load_all_fixtures(session, {
                "connectors": ConnectorModel,
                "knowledge_chunks": KnowledgeChunkModel
            })
        """
        for fixture_name, model_class in model_mapping.items():
            try:  # noqa: SIM105 -- explicit error handling preferred
                await self.load_fixture(fixture_name, session, model_class)
            except FileNotFoundError:
                # Skip missing fixtures
                pass

        await session.commit()


# Convenience function
async def load_fixtures(session: AsyncSession, model_mapping: dict[str, Any]) -> None:
    """
    Load multiple fixtures.

    Args:
        session: Database session
        model_mapping: Dict of fixture_name -> model_class

    Example:
        await load_fixtures(session, {
            "connectors": ConnectorModel,
            "knowledge_chunks": KnowledgeChunkModel
        })
    """
    loader = FixtureLoader()
    await loader.load_all_fixtures(session, model_mapping)
