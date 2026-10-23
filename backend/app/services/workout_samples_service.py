"""Per-workout samples for a workout detail view: downsampled series, route, laps and zones."""

from datetime import datetime, timedelta
from logging import Logger, getLogger
from typing import Any
from uuid import UUID

from app.database import DbSession
from app.models import DataPointSeries, EventRecord, WorkoutDetails
from app.repositories import DataPointSeriesRepository, DataSourceRepository, EventRecordRepository
from app.repositories.data_point_series_repository import WorkoutWindow
from app.schemas.enums import (
    GPS_STORAGE_SERIES,
    SCALED_GPS_SERIES,
    SeriesType,
    get_series_type_from_id,
    get_series_type_id,
    get_series_type_unit,
    to_public_value,
)
from app.schemas.responses.activity import (
    HeartRateZoneRead,
    PowerZoneRead,
    WorkoutLap,
    WorkoutSamples,
    WorkoutSampleSeries,
)
from app.schemas.utils import SourceMetadata

# Series a workout detail view can ask for. Latitude/longitude form the route, the rest are averaged.
WORKOUT_SAMPLE_TYPES: tuple[SeriesType, ...] = (
    SeriesType.heart_rate,
    SeriesType.speed,
    SeriesType.cadence,
    SeriesType.power,
    SeriesType.elevation,
    SeriesType.air_temperature,
    SeriesType.latitude,
    SeriesType.longitude,
)
ROUTE_TYPES: frozenset[SeriesType] = frozenset({SeriesType.latitude, SeriesType.longitude})

# FIT lap keys renamed to the response's unit-suffixed names; other lap keys pass through unchanged.
_LAP_KEY_RENAMES: dict[str, str] = {
    "elapsed_seconds": "duration_seconds",
    "avg_heart_rate": "avg_heart_rate_bpm",
    "max_heart_rate": "max_heart_rate_bpm",
    "avg_speed": "avg_speed_mps",
    "max_speed": "max_speed_mps",
}


class WorkoutSamplesService:
    def __init__(self, log: Logger):
        self.logger = log
        self.event_record_repo = EventRecordRepository(EventRecord)
        self.data_source_repo = DataSourceRepository()
        self.data_point_repo = DataPointSeriesRepository(DataPointSeries)

    def get_workout_samples(
        self,
        db_session: DbSession,
        user_id: UUID,
        workout_id: UUID,
        types: list[SeriesType],
        max_points: int,
    ) -> WorkoutSamples | None:
        """Samples, route, laps and zones of one workout, or None when it is not the user's workout."""
        record = self.event_record_repo.get_record_with_details(db_session, workout_id, "workout")
        if record is None or record.data_source_id is None:
            return None
        data_source = self.data_source_repo.get(db_session, record.data_source_id)
        if data_source is None or data_source.user_id != user_id:
            return None

        requested = [t for t in WORKOUT_SAMPLE_TYPES if t in (types or WORKOUT_SAMPLE_TYPES)]
        numeric_ids = [get_series_type_id(t) for t in requested if t not in ROUTE_TYPES]
        # Route rows: scaled storage types (current) and the legacy degree types (older rows).
        route_ids = [get_series_type_id(st) for t in requested if t in ROUTE_TYPES for st in (GPS_STORAGE_SERIES[t], t)]
        all_ids = numeric_ids + route_ids

        window = WorkoutWindow(
            user_id=user_id,
            start=record.start_datetime,
            end=record.end_datetime,
            provider=data_source.provider,
            source=data_source.source,
            data_source_id=data_source.id,
        )
        # Samples linked to the workout's own data source win; otherwise fall back to the
        # provider's other sources (Garmin stores workout samples on its device-less source).
        if all_ids and not self.data_point_repo.has_samples_in_window(db_session, window, all_ids):
            window = WorkoutWindow(
                user_id=user_id,
                start=record.start_datetime,
                end=record.end_datetime,
                provider=data_source.provider,
                source=data_source.source,
            )

        series: dict[str, WorkoutSampleSeries] = {}
        if numeric_ids:
            for type_id, ts, value in self.data_point_repo.get_bucketed_averages(
                db_session, window, numeric_ids, max_points
            ):
                series_type = get_series_type_from_id(type_id)
                entry = series.setdefault(
                    series_type.value, WorkoutSampleSeries(unit=get_series_type_unit(series_type))
                )
                entry.points.append((ts, round(value, 3)))

        route: list[tuple[datetime, float, float]] = []
        if len(route_ids) == 4:
            route = self._downsample_route(
                self.data_point_repo.get_raw_points(db_session, window, route_ids),
                record.start_datetime,
                record.end_datetime,
                max_points,
            )

        details = record.detail if isinstance(record.detail, WorkoutDetails) else None
        return WorkoutSamples(
            workout_id=record.id,
            start_time=record.start_datetime,
            end_time=record.end_datetime,
            source=SourceMetadata(provider=data_source.source or "unknown", device=data_source.device_model),
            series=series,
            route=route,
            laps=self._laps(details.segments if details else None),
            zones=self._zones(details),
        )

    @staticmethod
    def _downsample_route(
        rows: list[tuple[int, datetime, float]],
        start: datetime,
        end: datetime,
        max_points: int,
    ) -> list[tuple[datetime, float, float]]:
        """Pair latitude/longitude by timestamp and keep the recorded point nearest each bucket centre.

        Scaled rows win: when the window has any, legacy degree rows are ignored.
        """
        scaled_ids = {get_series_type_id(st) for st in SCALED_GPS_SERIES}
        if any(type_id in scaled_ids for type_id, _, _ in rows):
            rows = [row for row in rows if row[0] in scaled_ids]
        lats: dict[datetime, float] = {}
        lons: dict[datetime, float] = {}
        for type_id, ts, value in rows:
            stored = get_series_type_from_id(type_id)
            target = lats if stored in (SeriesType.latitude, SeriesType.latitude_e4) else lons
            target[ts] = round(to_public_value(stored, value), 7)
        points = sorted((ts, lat, lons[ts]) for ts, lat in lats.items() if ts in lons)
        if len(points) <= max_points:
            return points

        span = max((end - start).total_seconds(), 1.0)
        width = span / max_points
        best: dict[int, tuple[float, tuple[datetime, float, float]]] = {}
        for point in points:
            offset = (point[0] - start).total_seconds()
            bucket = min(int(offset / width), max_points - 1)
            distance = abs(offset - (bucket + 0.5) * width)
            if bucket not in best or distance < best[bucket][0]:
                best[bucket] = (distance, point)
        return [best[b][1] for b in sorted(best)]

    @staticmethod
    def _laps(segments: list[dict[str, Any]] | None) -> list[WorkoutLap]:
        laps: list[WorkoutLap] = []
        for segment in segments or []:
            if segment.get("kind") != "lap":
                continue
            lap = {_LAP_KEY_RENAMES.get(k, k): v for k, v in segment.items() if k != "kind"}
            start = lap.get("start_time")
            duration = lap.get("duration_seconds")
            if start and duration is not None:
                lap["end_time"] = datetime.fromisoformat(start) + timedelta(seconds=duration)
            laps.append(WorkoutLap(**lap))
        return laps

    @staticmethod
    def _zones(details: WorkoutDetails | None) -> dict[str, list[HeartRateZoneRead] | list[PowerZoneRead]]:
        zones: dict[str, list[HeartRateZoneRead] | list[PowerZoneRead]] = {}
        if details is None:
            return zones
        if details.hr_zones and details.hr_zones.get("zones"):
            hr: list[HeartRateZoneRead] = []
            previous_max: int | None = None
            for z in details.hr_zones["zones"]:
                hr.append(
                    HeartRateZoneRead(
                        zone=z["zone"], min_bpm=previous_max, max_bpm=z.get("max_bpm"), seconds=z["seconds"]
                    )
                )
                previous_max = z.get("max_bpm")
            zones["heart_rate"] = hr
        if details.power_zones and details.power_zones.get("zones"):
            power: list[PowerZoneRead] = []
            previous_max = None
            for z in details.power_zones["zones"]:
                power.append(
                    PowerZoneRead(
                        zone=z["zone"], min_watts=previous_max, max_watts=z.get("max_watts"), seconds=z["seconds"]
                    )
                )
                previous_max = z.get("max_watts")
            zones["power"] = power
        return zones


workout_samples_service = WorkoutSamplesService(log=getLogger(__name__))
