"""Delete a user's data that came from one provider, after that provider is disconnected.

Disconnecting a cloud provider (through the API, or the provider telling us the
user removed our app) queues :data:`DELETE_TASK`. The task removes every
``data_source`` row for that (user, provider) pair. Event records (workouts,
sleep, ...), their detail rows, time-series samples and archived daily
aggregates hang off ``data_source`` with ``ON DELETE CASCADE``, so they go with
it. Health scores carry their own ``provider`` column and are deleted directly.

Data from other providers, including Apple Health / SDK uploads that may mirror
the same device, is never touched: only rows whose provider is the disconnected
one are deleted.

Scheduling is best-effort: a broken broker never fails the disconnect itself.
"""

from logging import getLogger
from typing import Any
from uuid import UUID

import celery

from app.config import settings
from app.database import DbSession
from app.models import HealthScore
from app.repositories import DataSourceRepository, HealthScoreRepository, UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

DELETE_TASK = "app.integrations.celery.tasks.delete_provider_data_task.delete_provider_data"

data_source_repo = DataSourceRepository()
health_score_repo = HealthScoreRepository(HealthScore)
connection_repo = UserConnectionRepository()


def schedule_provider_data_deletion(user_id: UUID | str, provider: str, *, reason: str) -> bool:
    """Queue deletion of the user's data from ``provider``. Returns True if a task was queued."""
    if not settings.delete_provider_data_on_disconnect:
        return False
    try:
        celery.current_app.send_task(DELETE_TASK, args=[str(user_id), provider])
    except Exception:
        logger.warning("Could not queue provider data deletion for %s/%s", user_id, provider, exc_info=True)
        return False
    log_structured(
        logger,
        "info",
        "Queued provider data deletion",
        provider=provider,
        user_id=str(user_id),
        reason=reason,
    )
    return True


def delete_provider_data(db: DbSession, user_id: UUID, provider: str) -> dict[str, Any]:
    """Delete the user's data whose provider is ``provider``.

    Skipped when the user has an active connection to the provider again
    (they reconnected before the task ran): the data is theirs to keep.
    """
    provider_enum = ProviderName(provider)
    connection = connection_repo.get_by_user_and_provider(db, user_id, provider)
    if connection and connection.status == ConnectionStatus.ACTIVE:
        log_structured(
            logger,
            "info",
            "Skipped provider data deletion: connection is active again",
            provider=provider,
            user_id=str(user_id),
        )
        return {"status": "skipped_reconnected", "data_sources": 0, "health_scores": 0}

    health_scores = health_score_repo.delete_by_user_and_provider(db, user_id, provider_enum)
    data_sources = data_source_repo.delete_by_user_and_provider(db, user_id, provider_enum)
    db.commit()

    log_structured(
        logger,
        "info",
        "Deleted provider data after disconnect",
        provider=provider,
        user_id=str(user_id),
        data_sources=data_sources,
        health_scores=health_scores,
    )
    return {"status": "deleted", "data_sources": data_sources, "health_scores": health_scores}
