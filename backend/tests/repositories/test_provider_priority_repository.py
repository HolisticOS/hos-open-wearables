"""Tests for ProviderPriorityRepository."""

import pytest
from sqlalchemy.orm import Session

from app.models import ProviderPriority
from app.repositories.provider_priority_repository import ProviderPriorityRepository
from app.schemas.enums import ProviderName


class TestProviderPriorityRepository:
    """Test ProviderPriorityRepository methods."""

    @pytest.fixture
    def repo(self) -> ProviderPriorityRepository:
        """Create ProviderPriorityRepository instance."""
        return ProviderPriorityRepository(ProviderPriority)

    def test_ensure_provider_exists_creates_new(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """Should create a new provider priority if it doesn't exist."""
        # Arrange
        provider = ProviderName.APPLE

        # Act
        priority = repo.ensure_provider_exists(db, provider)

        # Assert
        assert priority is not None
        assert priority.provider == provider
        assert priority.priority == 20  # Default: Apple Health ranks below the wearables

        # Verify in database
        db_priority = db.query(ProviderPriority).filter_by(provider=provider).first()
        assert db_priority is not None
        assert db_priority.id == priority.id

    def test_ensure_provider_exists_returns_existing(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """Should return existing provider priority without creating duplicate."""
        # Arrange
        provider = ProviderName.GARMIN
        first_priority = repo.ensure_provider_exists(db, provider)
        original_id = first_priority.id

        # Act
        second_priority = repo.ensure_provider_exists(db, provider)

        # Assert
        assert second_priority.id == original_id
        assert second_priority.provider == provider

        # Verify only one record exists
        count = db.query(ProviderPriority).filter_by(provider=provider).count()
        assert count == 1

    def test_ensure_provider_exists_multiple_providers(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """Should handle multiple providers independently."""
        # Arrange & Act
        apple_priority = repo.ensure_provider_exists(db, ProviderName.APPLE)
        garmin_priority = repo.ensure_provider_exists(db, ProviderName.GARMIN)
        polar_priority = repo.ensure_provider_exists(db, ProviderName.POLAR)

        # Assert
        assert apple_priority.provider == ProviderName.APPLE
        assert garmin_priority.provider == ProviderName.GARMIN
        assert polar_priority.provider == ProviderName.POLAR

        # All should have different IDs
        assert len({apple_priority.id, garmin_priority.id, polar_priority.id}) == 3

    def test_get_all_returns_all_priorities(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """Should return all provider priorities ordered by priority."""
        # Arrange - create some priorities
        repo.ensure_provider_exists(db, ProviderName.APPLE)
        repo.ensure_provider_exists(db, ProviderName.GARMIN)
        repo.ensure_provider_exists(db, ProviderName.POLAR)

        # Act
        all_priorities = repo.get_all_ordered(db)

        # Assert
        assert len(all_priorities) >= 3
        providers = [p.provider for p in all_priorities]
        assert ProviderName.APPLE in providers
        assert ProviderName.GARMIN in providers
        assert ProviderName.POLAR in providers

        # Verify ordering by priority (ascending)
        priorities_values = [p.priority for p in all_priorities]
        assert priorities_values == sorted(priorities_values)

    def test_priority_order_defaults_put_wearables_ahead_of_apple(
        self, db: Session, repo: ProviderPriorityRepository
    ) -> None:
        """With no stored rows, a connected wearable still beats Apple Health."""
        order = repo.get_priority_order(db)

        assert order[ProviderName.GARMIN] < order[ProviderName.APPLE]
        assert order[ProviderName.WHOOP] < order[ProviderName.APPLE]

    def test_priority_order_stored_row_overrides_default(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """A priority someone set wins over the default; others keep their defaults."""
        repo.upsert(db, ProviderName.APPLE, 0)

        order = repo.get_priority_order(db)

        assert order[ProviderName.APPLE] == 0
        assert order[ProviderName.GARMIN] == 1

    def test_get_all_empty_database(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """Should return empty list when no priorities exist."""
        # Act
        all_priorities = repo.get_all_ordered(db)

        # Assert
        assert all_priorities == []

    def test_update_priority(self, db: Session, repo: ProviderPriorityRepository) -> None:
        """Should update provider priority value."""
        # Arrange
        provider = ProviderName.WHOOP
        priority_record = repo.ensure_provider_exists(db, provider)
        original_priority = priority_record.priority

        # Act - update priority
        new_priority_value = 10
        priority_record.priority = new_priority_value
        db.commit()
        db.refresh(priority_record)

        # Assert
        assert priority_record.priority == new_priority_value
        assert priority_record.priority != original_priority

        # Verify via repo
        updated = repo.ensure_provider_exists(db, provider)
        assert updated.priority == new_priority_value
