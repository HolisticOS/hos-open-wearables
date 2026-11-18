"""Celery task: ask Zepp to push a member's recent history.

Queued on connect (and by POST /users/{id}/sync/historical). Zepp answers a
backfill request with 202 and later delivers the data through the normal
webhook pushes, so this task only places the requests.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from celery import shared_task

from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.repositories import UserConnectionRepository
from app.schemas.enums import ProviderName
from app.services.providers.api_client import get_valid_token
from app.services.providers.factory import ProviderFactory
from app.services.providers.zepp.oauth import ZeppOAuth
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)


@shared_task(name=CeleryTask.ZEPP_REQUEST_BACKFILL)
def request_backfill(user_id: str, days: int) -> dict[str, Any]:
    if days <= 0:
        return {"user_id": user_id, "requested": {}}
    oauth = ProviderFactory().get_provider(ProviderName.ZEPP.value).oauth
    assert isinstance(oauth, ZeppOAuth)
    uid = UUID(user_id)
    with SessionLocal() as db:
        token = get_valid_token(db, uid, ProviderName.ZEPP.value, UserConnectionRepository(), oauth)
    end_s = int(datetime.now(timezone.utc).timestamp())
    start_s = end_s - days * 86400 + 1  # both ends inclusive, span ≤ 30 days
    requested = oauth.request_backfill(token, uid, start_s, end_s)
    logger.info("Zepp backfill requested", provider="zepp", user_id=user_id, days=days, requested=requested)
    return {"user_id": user_id, "requested": requested}
