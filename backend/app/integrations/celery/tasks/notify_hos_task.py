"""Celery task that tells the HOS backend new data landed for one user.

Queued by :func:`app.services.hos_notify.schedule_hos_notify`: once at once,
and once as the window's follow-up (``trailing``), which only sends if more
data arrived inside the window. The body carries the member-local ``dates``
the data touched when they are known, and leaves the field out otherwise.
"""

from typing import Any

import httpx
from celery import Task, shared_task

from app.config import settings
from app.services import hos_notify
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


@shared_task(
    name=hos_notify.NOTIFY_TASK,
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def notify_hos(
    self: Task,
    user_id: str,
    provider: str,
    trailing: bool = False,
    dates: list[str] | None = None,
) -> dict[str, Any]:
    url, key = settings.hos_notify_url, settings.hos_notify_service_key
    if not url or not key:
        return {"status": "disabled"}
    # A retry must not re-check the flag: the first attempt already took it, and passes the dates it took on.
    if trailing and self.request.retries == 0:
        if not hos_notify.take_pending(user_id):
            return {"status": "nothing_pending", "user_id": user_id}
        dates = hos_notify.take_pending_dates(user_id)

    dates = hos_notify.normalize_dates(dates)
    body: dict[str, Any] = {"ow_user_ids": [user_id], "provider": provider}
    if dates:
        body["dates"] = dates

    try:
        response = httpx.post(
            url,
            json=body,
            headers={"X-Service-Key": key.get_secret_value()},
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "HOS notify failed, scheduling retry",
            provider=provider,
            user_id=user_id,
            error=str(exc),
            attempt=self.request.retries,
        )
        raise self.retry(exc=exc, args=[user_id, provider, trailing, dates])

    logger.info("HOS notified", provider=provider, user_id=user_id, trailing=trailing, dates=dates)
    return {"status": "sent", "user_id": user_id, "dates": dates}
