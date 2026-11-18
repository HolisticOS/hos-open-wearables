from datetime import datetime, timedelta, timezone
from uuid import UUID

from celery import current_app as celery_app

from app.config import settings
from app.constants.celery_tasks import CeleryQueue, CeleryTask
from app.services.providers.base_strategy import (
    BaseProviderStrategy,
    HistoricalSyncResult,
    ProviderCapabilities,
    ProviderCoverage,
)
from app.services.providers.zepp.coverage import (
    HEALTH_SCORES,
    MENSTRUAL_CYCLE_FIELDS,
    SLEEP_FIELDS,
    TIMESERIES,
    WORKOUT_FIELDS,
)
from app.services.providers.zepp.data_247 import Zepp247Data
from app.services.providers.zepp.oauth import ZeppOAuth
from app.services.providers.zepp.webhook_handler import ZeppWebhookHandler

#: Zepp caps one backfill request at 30 days.
MAX_BACKFILL_DAYS = 30


class ZeppStrategy(BaseProviderStrategy):
    """Zepp (Amazfit) Open Platform: OAuth once, then Zepp pushes sleep, HRV and activity files."""

    def __init__(self) -> None:
        super().__init__()
        self.oauth = ZeppOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.data_247 = Zepp247Data(provider_name=self.name, api_base_url=self.api_base_url, oauth=self.oauth)
        self.webhooks = ZeppWebhookHandler(data_247=self.data_247)

    @property
    def name(self) -> str:
        return "zepp"

    @property
    def api_base_url(self) -> str:
        return "https://api-open.zepp.com"

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Full records in every push; history is requested by REST and answered by pushes.
        return ProviderCapabilities(webhook_stream=True, webhook_callback=True, max_historical_days=MAX_BACKFILL_DAYS)

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            menstrual_cycle_fields=MENSTRUAL_CYCLE_FIELDS,
            health_scores=HEALTH_SCORES,
        )

    def start_historical_sync(self, user_id: UUID, days: int) -> HistoricalSyncResult:
        days = min(days, MAX_BACKFILL_DAYS, settings.zepp_backfill_days)
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)
        task = celery_app.send_task(
            CeleryTask.ZEPP_REQUEST_BACKFILL, args=[str(user_id), days], queue=CeleryQueue.WEBHOOK_SYNC
        )
        return HistoricalSyncResult(
            task_id=task.id,
            method="webhook_backfill",
            message=f"Zepp {days}-day backfill requested; the data arrives as webhook pushes.",
            days=days,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
