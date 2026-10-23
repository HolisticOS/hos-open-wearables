"""Tests for deleting a user's data from one provider after it is disconnected.

Only rows whose provider is the disconnected one may go: the same user's data
from other providers (Apple Health included) and other users' data stay.
"""

from collections.abc import Generator
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.celery.tasks.delete_provider_data_task import delete_provider_data as delete_task
from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, User, WorkoutDetails
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName
from app.services import provider_data_deletion
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    UserConnectionFactory,
    UserFactory,
    WorkoutDetailsFactory,
)


def _seed(user: User, provider: ProviderName) -> DataSource:
    """One data source with a workout (+detail), a sample and a health score."""
    source = DataSourceFactory(user=user, provider=provider, source=provider.value)
    record = EventRecordFactory(data_source=source)
    WorkoutDetailsFactory(event_record=record)
    DataPointSeriesFactory(data_source=source)
    HealthScoreFactory(data_source=source, provider=provider)
    return source


def _counts(db: Session, source_id: UUID) -> tuple[int, int, int]:
    return (
        db.query(EventRecord).filter(EventRecord.data_source_id == source_id).count(),
        db.query(DataPointSeries).filter(DataPointSeries.data_source_id == source_id).count(),
        db.query(HealthScore).filter(HealthScore.data_source_id == source_id).count(),
    )


class TestDeleteProviderData:
    def test_deletes_only_that_providers_data_for_that_user(self, db: Session) -> None:
        user = UserFactory()
        other_user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.REVOKED)
        garmin = _seed(user, ProviderName.GARMIN)
        apple = _seed(user, ProviderName.APPLE)
        polar = _seed(user, ProviderName.POLAR)
        others_garmin = _seed(other_user, ProviderName.GARMIN)
        # A score the provider reported without a data source row still belongs to that provider.
        HealthScoreFactory(data_source_id=None, user_id=user.id, provider=ProviderName.GARMIN)
        garmin_id = garmin.id
        garmin_record_ids = [r.id for r in db.query(EventRecord).filter(EventRecord.data_source_id == garmin_id)]

        result = provider_data_deletion.delete_provider_data(db, user.id, "garmin")

        assert result["status"] == "deleted"
        assert result["data_sources"] == 1
        assert result["health_scores"] == 2
        db.expire_all()
        assert db.query(DataSource).filter(DataSource.id == garmin_id).count() == 0
        assert _counts(db, garmin_id) == (0, 0, 0)
        assert db.query(WorkoutDetails).filter(WorkoutDetails.record_id.in_(garmin_record_ids)).count() == 0
        assert db.query(HealthScore).filter_by(user_id=user.id, provider=ProviderName.GARMIN).count() == 0
        # Everything else is untouched.
        assert _counts(db, apple.id) == (1, 1, 1)
        assert _counts(db, polar.id) == (1, 1, 1)
        assert _counts(db, others_garmin.id) == (1, 1, 1)

    def test_skips_when_connection_is_active_again(self, db: Session) -> None:
        """The user reconnected before the task ran: their data stays."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.ACTIVE)
        garmin = _seed(user, ProviderName.GARMIN)

        result = provider_data_deletion.delete_provider_data(db, user.id, "garmin")

        assert result["status"] == "skipped_reconnected"
        assert _counts(db, garmin.id) == (1, 1, 1)

    def test_no_connection_row_still_deletes(self, db: Session) -> None:
        user = UserFactory()
        garmin = _seed(user, ProviderName.GARMIN)
        garmin_id = garmin.id

        result = provider_data_deletion.delete_provider_data(db, user.id, "garmin")

        assert result["data_sources"] == 1
        db.expire_all()
        assert _counts(db, garmin_id) == (0, 0, 0)


@pytest.fixture
def celery_app() -> Generator[MagicMock, None, None]:
    with patch("celery.current_app") as app:
        yield app


class TestScheduleProviderDataDeletion:
    def test_queues_task(self, celery_app: MagicMock) -> None:
        with patch.object(settings, "delete_provider_data_on_disconnect", True):
            queued = provider_data_deletion.schedule_provider_data_deletion("u-1", "garmin", reason="disconnect")

        assert queued is True
        celery_app.send_task.assert_called_once_with(provider_data_deletion.DELETE_TASK, args=["u-1", "garmin"])

    def test_setting_off_queues_nothing(self, celery_app: MagicMock) -> None:
        with patch.object(settings, "delete_provider_data_on_disconnect", False):
            queued = provider_data_deletion.schedule_provider_data_deletion("u-1", "garmin", reason="disconnect")

        assert queued is False
        celery_app.send_task.assert_not_called()

    def test_broker_failure_never_raises(self, celery_app: MagicMock) -> None:
        celery_app.send_task.side_effect = ConnectionError("broker down")

        with patch.object(settings, "delete_provider_data_on_disconnect", True):
            queued = provider_data_deletion.schedule_provider_data_deletion("u-1", "garmin", reason="disconnect")

        assert queued is False


class TestDeleteProviderDataTask:
    def test_task_deletes_with_own_session(self, db: Session) -> None:
        user = UserFactory()
        garmin = _seed(user, ProviderName.GARMIN)
        garmin_id = garmin.id
        session_cm = MagicMock()
        session_cm.__enter__.return_value = db

        with patch("app.integrations.celery.tasks.delete_provider_data_task.SessionLocal", return_value=session_cm):
            result = delete_task.run(str(user.id), "garmin")

        assert result["status"] == "deleted"
        assert result["provider"] == "garmin"
        db.expire_all()
        assert _counts(db, garmin_id) == (0, 0, 0)

    def test_task_retries_on_failure(self) -> None:
        with (
            patch(
                "app.integrations.celery.tasks.delete_provider_data_task.provider_data_deletion.delete_provider_data",
                side_effect=RuntimeError("db down"),
            ),
            patch("app.integrations.celery.tasks.delete_provider_data_task.SessionLocal"),
            patch.object(delete_task, "retry", side_effect=Retry()) as retry,
            pytest.raises(Retry),
        ):
            delete_task.run("00000000-0000-0000-0000-000000000001", "garmin")

        retry.assert_called_once()
