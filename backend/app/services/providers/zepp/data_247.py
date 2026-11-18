"""Zepp (Amazfit) 247 data.

Zepp's push payloads copy Garmin's Health API field for field (``sleeps``,
``hrv``, ``dailies``, ``bodyComps``, ``skinTemp`` ...), so this reuses
``Garmin247Data`` and only handles what differs:

- ``summaryId`` may be null on sleep and HRV. Zepp's documented identity is
  ``userId + calendarDate + startTimeInSeconds``, so we key on that instead.
- Pulse ox values arrive under ``timeOffsetPulseOxValues``, not Garmin's
  ``timeOffsetSpo2Values``.
- An activity is only a ping with a one-time FIT download link (24 h): no
  ``activityDetails`` creates the workout first, so the workout itself is
  built from the FIT file's session totals.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.constants.workout_types.garmin import get_unified_workout_type
from app.database import DbSession
from app.repositories.data_point_series_repository import WriteCounts
from app.schemas.enums import EventCategory
from app.schemas.model_crud.activities import EventRecordCreate, EventRecordDetailCreate, TimeSeriesSampleCreate
from app.services.event_record_service import event_record_service
from app.services.fit_parser import FitParseResult, parse_fit_file
from app.services.providers.api_client import download_binary_content
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.zepp.coverage import WORKOUT_ENERGY_SERIES
from app.services.raw_payload_storage import store_fit_file
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

#: Zepp push key → the Garmin summary type whose builder handles it.
ZEPP_TO_GARMIN_TYPE: dict[str, str] = {
    "sleeps": "sleeps",
    "hrv": "hrv",
    "dailies": "dailies",
    "pulseOx": "pulseox",
    "bodyComps": "bodyComps",
    "skinTemp": "skinTemp",
    "activityFiles": "activityFiles",
}


def with_identity(summary_type: str, item: dict[str, Any]) -> dict[str, Any]:
    """The item with a stable ``summaryId``, and Garmin's field names where Zepp's differ."""
    item = dict(item)
    if not item.get("summaryId"):
        start = item.get("startTimeInSeconds", item.get("measurementTimeInSeconds"))
        item["summaryId"] = f"{summary_type}-{item.get('calendarDate')}-{start}"
    if summary_type == "pulseox" and "timeOffsetSpo2Values" not in item:
        item["timeOffsetSpo2Values"] = item.get("timeOffsetPulseOxValues") or {}
    return item


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


class Zepp247Data(Garmin247Data):
    source_label = "Zepp"

    def process_items_batch(
        self,
        db: DbSession,
        user_id: UUID,
        summary_type: str,
        items: list[dict[str, Any]],
    ) -> WriteCounts:
        items = [with_identity(summary_type, item) for item in items]
        if summary_type == "activityFiles":
            return self._save_fit_workouts(db, user_id, items)
        return super().process_items_batch(db, user_id, summary_type, items)

    # -------------------------------------------------------------------------
    # Workouts from FIT files
    # -------------------------------------------------------------------------

    def build_fit_workout(
        self, user_id: UUID, ping: dict[str, Any], fit: FitParseResult
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate] | None:
        """EventRecord + workout detail from the ping and its FIT session (no DB interaction)."""
        session = fit.session or {}
        start_ts = ping.get("startTimeInSeconds")
        start_dt = (
            datetime.fromtimestamp(int(start_ts), tz=timezone.utc)
            if start_ts
            else (datetime.fromisoformat(session["start_time"]) if session.get("start_time") else None)
        )
        elapsed = session.get("total_elapsed_time") or session.get("total_timer_time")
        if start_dt is None or not elapsed:
            return None
        duration = int(round(elapsed))
        activity_type = ping.get("activityType") or (session.get("sport") or "unknown").upper()

        record_id = uuid4()
        record = EventRecordCreate(
            id=record_id,
            category=EventCategory.WORKOUT,
            type=get_unified_workout_type(activity_type).value.lower(),
            source_name=self.source_label,
            device_model=ping.get("deviceName"),
            duration_seconds=duration,
            start_datetime=start_dt,
            end_datetime=start_dt + timedelta(seconds=duration),
            zone_offset=None,
            external_id=str(ping.get("summaryId") or ping.get("activityId")),
            source=self.provider_name,
            user_id=user_id,
        )
        max_hr = session.get("max_heart_rate")
        timer = session.get("total_timer_time")
        detail = EventRecordDetailCreate(
            record_id=record_id,
            distance=_dec(session.get("total_distance")),
            energy_burned=_dec(session.get("total_calories")),
            energy_series=WORKOUT_ENERGY_SERIES,
            heart_rate_avg=_dec(session.get("avg_heart_rate")),
            heart_rate_max=int(max_hr) if max_hr is not None else None,
            total_elevation_gain=_dec(session.get("total_ascent")),
            average_speed=_dec(session.get("avg_speed")),
            max_speed=_dec(session.get("max_speed")),
            average_cadence=_dec(session.get("avg_cadence")),
            moving_time_seconds=int(round(timer)) if timer is not None else None,
            segments=fit.segments or None,
            hr_zones=fit.hr_zones,
            power_zones=fit.power_zones,
        )
        return record, detail

    def _save_fit_workouts(self, db: DbSession, user_id: UUID, pings: list[dict[str, Any]]) -> WriteCounts:
        records: list[EventRecordCreate] = []
        details: list[EventRecordDetailCreate] = []
        samples: list[TimeSeriesSampleCreate] = []

        for ping in pings:
            external_id = str(ping.get("summaryId") or ping.get("activityId"))
            callback_url = ping.get("callbackURL")
            if ping.get("fileType", "FIT") != "FIT" or not callback_url:
                continue
            # Zepp retries and duplicates pings; the link is one-time, so don't spend it twice.
            if self.event_record_repo.get_by_external_id(db, user_id, external_id, source=self.provider_name):
                continue
            try:
                fit_bytes = download_binary_content(
                    db=db,
                    user_id=user_id,
                    connection_repo=self.connection_repo,
                    oauth=self.oauth,
                    provider_name=self.provider_name,
                    url=callback_url,
                )
            except Exception as e:
                # 410 = the 24 h link expired; nothing to retry.
                logger.warning("Failed to download Zepp FIT file", provider="zepp", user_id=str(user_id), exc_info=e)
                continue
            store_fit_file(
                provider=self.provider_name, fit_bytes=fit_bytes, user_id=str(user_id), activity_id=external_id
            )
            try:
                fit = parse_fit_file(fit_bytes, user_id, source=self.provider_name)
            except Exception as e:
                logger.warning("Failed to parse Zepp FIT file", provider="zepp", user_id=str(user_id), exc_info=e)
                continue
            built = self.build_fit_workout(user_id, ping, fit)
            if built is None:
                logger.warning("Zepp FIT file had no session totals", provider="zepp", user_id=str(user_id))
                continue
            records.append(built[0])
            details.append(built[1])
            if settings.ingest_workout_samples:
                samples.extend(fit.samples)

        count = WriteCounts(0, 0)
        if records:
            inserted = set(event_record_service.bulk_create(db, records))
            db.flush()
            kept = [d for d in details if d.record_id in inserted]
            if kept:
                event_record_service.bulk_create_details(db, kept, detail_type=EventCategory.WORKOUT)
            count += WriteCounts(len(inserted), 0)
        if samples:
            count += self.data_point_repo.bulk_create(db, samples)
        return count
