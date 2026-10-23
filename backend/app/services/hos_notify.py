"""Tell the HOS backend that new data landed for a user.

Ingestion (webhook pushes, SDK uploads) calls :func:`schedule_hos_notify` after
it commits. The first call for a user notifies HOS straight away and opens a
short window. Calls inside the window only mark the user as pending; one
follow-up notify at the end of the window covers all of them. So a Garmin
burst (dailies, epochs, sleep, stress... each its own webhook) costs HOS two
refreshes, and the first one is never held back.

Best-effort throughout: a broken Redis or HOS never fails ingestion.
"""

from collections.abc import Iterable
from logging import getLogger
from uuid import UUID

from app.config import settings
from app.integrations.redis_client import get_redis_client

logger = getLogger(__name__)

NOTIFY_TASK = "app.integrations.celery.tasks.notify_hos_task.notify_hos"


def is_enabled() -> bool:
    return bool(settings.hos_notify_url and settings.hos_notify_service_key)


def _window_key(user_id: str) -> str:
    return f"hos_notify:window:{user_id}"


def _pending_key(user_id: str) -> str:
    return f"hos_notify:pending:{user_id}"


def take_pending(user_id: str) -> bool:
    """True (once) if data arrived for the user after the window's first notify."""
    try:
        return get_redis_client().getdel(_pending_key(user_id)) is not None
    except Exception:
        logger.warning("hos_notify: could not read pending flag for %s", user_id, exc_info=True)
        return True  # unsure, so send: an extra refresh is cheaper than a missed one


def schedule_hos_notify(user_ids: Iterable[UUID | str], provider: str) -> None:
    if not is_enabled():
        return

    from celery import current_app as celery_app

    window = settings.hos_notify_window_seconds
    for raw in {str(u) for u in user_ids if u}:
        try:
            redis = get_redis_client()
            if redis.set(_window_key(raw), provider, nx=True, ex=window):
                celery_app.send_task(NOTIFY_TASK, args=[raw, provider, False])
                celery_app.send_task(NOTIFY_TASK, args=[raw, provider, True], countdown=window)
            else:
                redis.set(_pending_key(raw), provider, ex=window * 6)
        except Exception:
            logger.warning("hos_notify: could not schedule notify for %s", raw, exc_info=True)
