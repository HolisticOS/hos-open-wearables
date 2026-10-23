"""Celery task that tells the HOS backend new data landed for one user.

Queued by :func:`app.services.hos_notify.schedule_hos_notify`: once at once,
and once as the window's follow-up (``trailing``), which only sends if more
data arrived inside the window.
"""

from logging import getLogger
from typing import Any

import httpx
from celery import Task, shared_task

from app.config import settings
from app.services import hos_notify
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task(
    name=hos_notify.NOTIFY_TASK,
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def notify_hos(self: Task, user_id: str, provider: str, trailing: bool = False) -> dict[str, Any]:
    url, key = settings.hos_notify_url, settings.hos_notify_service_key
    if not url or not key:
        return {"status": "disabled"}
    # A retry must not re-check the flag: the first attempt already took it.
    if trailing and self.request.retries == 0 and not hos_notify.take_pending(user_id):
        return {"status": "nothing_pending", "user_id": user_id}

    try:
        response = httpx.post(
            url,
            json={"ow_user_ids": [user_id], "provider": provider},
            headers={"X-Service-Key": key.get_secret_value()},
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log_structured(
            logger,
            "warning",
            "HOS notify failed, scheduling retry",
            provider=provider,
            user_id=user_id,
            error=str(exc),
            attempt=self.request.retries,
        )
        raise self.retry(exc=exc)

    log_structured(logger, "info", "HOS notified", provider=provider, user_id=user_id, trailing=trailing)
    return {"status": "sent", "user_id": user_id}
