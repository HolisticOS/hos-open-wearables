"""Celery periodic task that keeps Garmin refresh tokens alive.

A push-only Garmin integration never calls the Garmin API after connecting,
so nothing refreshes its tokens. The refresh token then expires and later
deregistration (on disconnect or user deletion) is impossible. Once a day this
task refreshes every active Garmin connection whose access token has expired
or will expire before the next run, which also rotates the refresh token.

A rejected refresh is logged and the connection stays active. Garmin keeps
pushing data to a connection whatever its tokens, so revoking it here dropped
every push until the member reconnected (2026-10-27: a 401 on each nightly run
revoked the link and the day's strength session was discarded). The member's
reconnect, or Garmin's own deregistration push, is what ends a connection.
"""

from datetime import timedelta
from logging import getLogger
from typing import Any

from celery import shared_task

from app.database import SessionLocal
from app.repositories import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.services.providers.factory import ProviderFactory
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

TASK_NAME = "app.integrations.celery.tasks.garmin.token_refresh_task.refresh_garmin_tokens"

# Runs daily: refresh anything that would otherwise expire before the next run.
REFRESH_WINDOW = timedelta(days=1, minutes=10)


@shared_task(name=TASK_NAME)
def refresh_garmin_tokens() -> dict[str, Any]:
    """Refresh Garmin connections whose access token is expired or close to expiry."""
    oauth = ProviderFactory().get_provider(ProviderName.GARMIN.value).oauth
    if oauth is None:
        return {"refreshed": 0, "failed": 0, "candidates": 0}

    refreshed = 0
    failed = 0
    with SessionLocal() as db:
        connections = UserConnectionRepository().get_expiring_tokens(
            db,
            minutes_threshold=int(REFRESH_WINDOW.total_seconds() // 60),
            provider=ProviderName.GARMIN.value,
            require_refresh_token=True,
        )
        for connection in connections:
            refresh_token = connection.refresh_token
            if not refresh_token:
                continue
            try:
                oauth.refresh_access_token(db, connection.user_id, refresh_token, revoke_on_reject=False)
                refreshed += 1
            except Exception as e:
                db.rollback()
                failed += 1
                log_and_capture_error(
                    e,
                    logger,
                    f"Failed to refresh Garmin token for user {connection.user_id}: {e}",
                    extra={"user_id": str(connection.user_id), "provider": ProviderName.GARMIN.value},
                )

    log_structured(
        logger,
        "info",
        "Garmin token refresh run complete",
        provider=ProviderName.GARMIN.value,
        candidates=len(connections),
        refreshed=refreshed,
        failed=failed,
    )
    return {"refreshed": refreshed, "failed": failed, "candidates": len(connections)}
