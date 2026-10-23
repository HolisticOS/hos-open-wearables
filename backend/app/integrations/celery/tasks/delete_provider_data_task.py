"""Celery task: delete a user's data from one provider after it was disconnected.

Queued by :func:`app.services.provider_data_deletion.schedule_provider_data_deletion`.
"""

from logging import getLogger
from typing import Any
from uuid import UUID

from celery import Task, shared_task

from app.database import SessionLocal
from app.services import provider_data_deletion
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(
    name=provider_data_deletion.DELETE_TASK,
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    default_retry_delay=600,  # 10 minutes; deletion only has to land within days
)
def delete_provider_data(self: Task, user_id: str, provider: str) -> dict[str, Any]:
    """Delete the user's data whose provider is ``provider``."""
    try:
        with SessionLocal() as db:
            result = provider_data_deletion.delete_provider_data(db, UUID(user_id), provider)
    except Exception as exc:
        log_structured(
            logger,
            "error",
            "Provider data deletion failed, scheduling retry",
            provider=provider,
            user_id=user_id,
            error=str(exc),
            attempt=self.request.retries,
        )
        raise self.retry(exc=exc)
    return {"user_id": user_id, "provider": provider, **result}
