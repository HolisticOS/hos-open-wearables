"""Tell the HOS backend that new data landed for a user.

Ingestion (webhook pushes, SDK uploads) calls :func:`schedule_hos_notify` after
it commits. The first call for a user notifies HOS straight away and opens a
short window. Calls inside the window only mark the user as pending; one
follow-up notify at the end of the window covers all of them. So a Garmin
burst (dailies, epochs, sleep, stress... each its own webhook) costs HOS two
refreshes, and the first one is never held back.

A caller that knows which member-local calendar days its data touched passes
them as ``dates``; the notify body then carries ``"dates": ["YYYY-MM-DD", ...]``
(deduped, sorted, the ``MAX_NOTIFY_DATES`` most recent) so HOS refreshes only
those days. The field is optional: a notify that covers any call without dates
leaves it out, and HOS refreshes as before.

Best-effort throughout: a broken Redis or HOS never fails ingestion.
"""

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from app.config import settings
from app.integrations.redis_client import get_redis_client
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

NOTIFY_TASK = "app.integrations.celery.tasks.notify_hos_task.notify_hos"

# HOS refreshes a day per date, and a re-sent trailing window (the app re-reads 48 h of sleep) is a few days at most.
MAX_NOTIFY_DATES = 7

# Stored in the pending-dates set by a call inside the window that did not say which days it touched.
_ANY_DATE = "*"


def is_enabled() -> bool:
    return bool(settings.hos_notify_url and settings.hos_notify_service_key)


def _window_key(user_id: str) -> str:
    return f"hos_notify:window:{user_id}"


def _pending_key(user_id: str) -> str:
    return f"hos_notify:pending:{user_id}"


def _pending_dates_key(user_id: str) -> str:
    return f"hos_notify:pending_dates:{user_id}"


def normalize_dates(dates: Iterable[date | str] | None) -> list[str] | None:
    """ISO dates, deduped and sorted, the MAX_NOTIFY_DATES most recent; None when there are none."""
    if dates is None:
        return None
    iso = {d.isoformat() if isinstance(d, date) else str(d) for d in dates if d}
    return sorted(iso)[-MAX_NOTIFY_DATES:] or None


def take_pending(user_id: str) -> bool:
    """True (once) if data arrived for the user after the window's first notify."""
    try:
        return get_redis_client().getdel(_pending_key(user_id)) is not None
    except Exception:
        logger.warning("hos_notify: could not read pending flag", user_id=user_id, exc_info=True)
        return True  # unsure, so send: an extra refresh is cheaper than a missed one


def take_pending_dates(user_id: str) -> list[str] | None:
    """The days the window's pending calls touched (once), or None when any of them did not say."""
    try:
        pipe = get_redis_client().pipeline()
        pipe.smembers(_pending_dates_key(user_id))
        pipe.delete(_pending_dates_key(user_id))
        raw, _ = pipe.execute()
    except Exception:
        logger.warning("hos_notify: could not read pending dates", user_id=user_id, exc_info=True)
        return None  # unsure, so refresh everything
    members = {m.decode() if isinstance(m, bytes) else str(m) for m in raw or ()}
    if not members or _ANY_DATE in members:
        return None
    return normalize_dates(members)


def schedule_hos_notify(
    user_ids: Iterable[UUID | str],
    provider: str,
    dates: Iterable[date | str] | None = None,
) -> None:
    """Notify HOS for each user, with the member-local days the data touched when the caller knows them."""
    if not is_enabled():
        return

    from celery import current_app as celery_app

    window = settings.hos_notify_window_seconds
    day_list = normalize_dates(dates)
    for raw in {str(u) for u in user_ids if u}:
        try:
            redis = get_redis_client()
            if redis.set(_window_key(raw), provider, nx=True, ex=window):
                # Dates only when known, so a worker still on the three-argument task keeps working.
                args = [raw, provider, False, day_list] if day_list else [raw, provider, False]
                celery_app.send_task(NOTIFY_TASK, args=args)
                celery_app.send_task(NOTIFY_TASK, args=[raw, provider, True], countdown=window)
            else:
                pipe = redis.pipeline()
                pipe.set(_pending_key(raw), provider, ex=window * 6)
                pipe.sadd(_pending_dates_key(raw), *(day_list or [_ANY_DATE]))
                pipe.expire(_pending_dates_key(raw), window * 6)
                pipe.execute()
        except Exception:
            logger.warning("hos_notify: could not schedule notify", user_id=str(raw), exc_info=True)
