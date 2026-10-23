"""Tests for GET /api/v1/users/{user_id}/events/workouts/{workout_id}/samples."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, EventRecord
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from tests.factories import ApiKeyFactory, DataSourceFactory, EventRecordFactory, UserFactory, WorkoutDetailsFactory
from tests.utils import api_key_headers

START = datetime(2025, 11, 20, 7, 0, tzinfo=timezone.utc)
START_TS = int(START.timestamp())
DURATION = 3600  # one hour, one sample per second


def _activity_details(seconds: int = DURATION) -> dict[str, Any]:
    """A Garmin activityDetails item (summary + per-second samples) using the repo's sample field names."""
    return {
        "activityId": 21047282990,
        "summary": {
            "activityId": 21047282990,
            "activityType": "RUNNING",
            "deviceName": "Forerunner 965",
            "startTimeInSeconds": START_TS,
            "startTimeOffsetInSeconds": 3600,
            "durationInSeconds": seconds,
        },
        "samples": [
            {
                "startTimeInSeconds": START_TS + i,
                "heartRate": 120 + i % 40,
                "speedMetersPerSecond": 2.5 + (i % 10) / 10,
                "elevationInMeters": 100 + i % 7,
                # value column keeps 3 decimals, so use coordinates it can hold exactly
                "latitudeInDegree": round(52.0 + i * 0.001, 3),
                "longitudeInDegree": round(21.0 + i * 0.001, 3),
            }
            for i in range(seconds)
        ],
    }


@pytest.fixture
def garmin_247() -> Garmin247Data:
    oauth = GarminOAuth(
        user_repo=MagicMock(),
        connection_repo=UserConnectionRepository(),
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
    )
    return Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=oauth)


def _garmin_workout(db: Session, garmin_247: Garmin247Data, user: Any) -> EventRecord:
    """Workout record on the device's data source; samples land on the device-less Garmin source, as in prod."""
    data_source = DataSourceFactory(
        user=user, provider=ProviderName.GARMIN, source="garmin", device_model="Forerunner 965", device_type=None
    )
    workout = EventRecordFactory(
        data_source=data_source,
        category="workout",
        type="running",
        source_name="Forerunner 965",
        duration_seconds=DURATION,
        start_datetime=START,
        end_datetime=START + timedelta(seconds=DURATION),
        external_id="21047282990",
    )
    WorkoutDetailsFactory(
        event_record=workout,
        distance=Decimal("10000"),
        heart_rate_avg=Decimal("139"),
        segments=[
            {
                "kind": "lap",
                "index": 0,
                "start_time": START.isoformat(),
                "elapsed_seconds": 1800.0,
                "distance_meters": 5000.0,
                "avg_heart_rate": 138.0,
                "max_heart_rate": 159.0,
                "avg_speed": 2.78,
                "avg_cadence": 84.0,
                "total_ascent": 12.0,
            },
            {
                "kind": "lap",
                "index": 1,
                "start_time": (START + timedelta(seconds=1800)).isoformat(),
                "elapsed_seconds": 1800.0,
                "distance_meters": 5000.0,
                "avg_heart_rate": 141.0,
                "avg_speed": 2.78,
            },
            {"kind": "split", "index": 0, "start_time": START.isoformat(), "elapsed_seconds": 600.0},
        ],
        hr_zones={
            "zones": [
                {"zone": 0, "seconds": 120.0, "max_bpm": 110},
                {"zone": 1, "seconds": 900.0, "max_bpm": 140},
                {"zone": 2, "seconds": 2580.0, "max_bpm": None},
            ],
            "max_hr": 190,
            "threshold_hr": 170,
        },
    )
    samples = garmin_247._build_activity_samples(user.id, _activity_details())
    DataPointSeriesRepository(DataPointSeries).bulk_create(db, samples)
    db.flush()
    return workout


def _apple_heart_rate(db: Session, user: Any, value: int) -> DataSource:
    """Apple Health heart rate recorded during the same hour (must never mix into a Garmin workout)."""
    samples = [
        TimeSeriesSampleCreate(
            id=uuid4(),
            user_id=user.id,
            source="apple_health_sdk",
            provider="apple",
            device_model="Apple Watch",
            recorded_at=START + timedelta(seconds=i),
            value=Decimal(value),
            series_type=SeriesType.heart_rate,
        )
        for i in range(0, DURATION, 10)
    ]
    DataPointSeriesRepository(DataPointSeries).bulk_create(db, samples)
    db.flush()
    return db.query(DataSource).filter(DataSource.user_id == user.id, DataSource.provider == ProviderName.APPLE).one()


class TestWorkoutSamplesEndpoint:
    def _get(self, client: TestClient, user_id: Any, workout_id: Any, **params: Any) -> Any:
        return client.get(
            f"/api/v1/users/{user_id}/events/workouts/{workout_id}/samples",
            headers=api_key_headers(ApiKeyFactory().id),
            params=params,
        )

    def test_shapes_and_downsampling(self, client: TestClient, db: Session, garmin_247: Garmin247Data) -> None:
        user = UserFactory()
        workout = _garmin_workout(db, garmin_247, user)
        _apple_heart_rate(db, user, value=230)

        response = self._get(client, user.id, workout.id)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["workout_id"] == str(workout.id)
        assert body["source"] == {"provider": "garmin", "device": "Forerunner 965"}
        assert set(body["series"]) == {"heart_rate", "speed", "elevation"}  # only what was recorded

        hr = body["series"]["heart_rate"]
        assert hr["unit"] == "bpm"
        assert 590 <= len(hr["points"]) <= 600  # 3600 per-second samples into at most 600 buckets
        assert all(120 <= value <= 159 for _, value in hr["points"])  # Apple's 230 bpm never mixed in
        timestamps = [datetime.fromisoformat(ts) for ts, _ in hr["points"]]
        assert timestamps == sorted(timestamps)
        assert timestamps[0] >= START
        assert timestamps[-1] <= START + timedelta(seconds=DURATION)

        route = body["route"]
        assert 590 <= len(route) <= 600
        recorded = {(round(52.0 + i * 0.001, 3), round(21.0 + i * 0.001, 3)) for i in range(DURATION)}
        assert all((lat, lon) in recorded for _, lat, lon in route)  # real points, never averaged

        laps = body["laps"]
        assert [lap["index"] for lap in laps] == [0, 1]  # splits are not laps
        first = laps[0]
        assert first["duration_seconds"] == 1800.0
        assert first["distance_meters"] == 5000.0
        assert first["avg_heart_rate_bpm"] == 138.0
        assert first["max_heart_rate_bpm"] == 159.0
        assert first["avg_speed_mps"] == 2.78
        assert first["avg_cadence"] == 84.0  # passed through from the FIT lap
        assert datetime.fromisoformat(first["end_time"]) == START + timedelta(seconds=1800)

        assert body["zones"] == {
            "heart_rate": [
                {"zone": 0, "min_bpm": None, "max_bpm": 110, "seconds": 120.0},
                {"zone": 1, "min_bpm": 110, "max_bpm": 140, "seconds": 900.0},
                {"zone": 2, "min_bpm": 140, "max_bpm": None, "seconds": 2580.0},
            ]
        }

    def test_max_points_and_types(self, client: TestClient, db: Session, garmin_247: Garmin247Data) -> None:
        user = UserFactory()
        workout = _garmin_workout(db, garmin_247, user)

        body = self._get(client, user.id, workout.id, types=["heart_rate"], max_points=50).json()

        assert set(body["series"]) == {"heart_rate"}
        assert 45 <= len(body["series"]["heart_rate"]["points"]) <= 50
        assert body["route"] == []  # latitude/longitude not requested

        route_only = self._get(client, user.id, workout.id, types=["latitude", "longitude"], max_points=2000).json()
        assert route_only["series"] == {}
        assert len(route_only["route"]) == 2000

    def test_own_data_source_wins(self, client: TestClient, db: Session, garmin_247: Garmin247Data) -> None:
        """An Apple workout with samples on its own source returns only those, not Garmin's."""
        user = UserFactory()
        _garmin_workout(db, garmin_247, user)
        apple_source = _apple_heart_rate(db, user, value=99)
        apple_workout = EventRecordFactory(
            data_source=apple_source,
            category="workout",
            type="running",
            duration_seconds=DURATION,
            start_datetime=START,
            end_datetime=START + timedelta(seconds=DURATION),
        )

        body = self._get(client, user.id, apple_workout.id).json()

        assert set(body["series"]) == {"heart_rate"}
        assert {value for _, value in body["series"]["heart_rate"]["points"]} == {99.0}
        assert body["route"] == []
        assert body["laps"] == []
        assert body["zones"] == {}

    def test_not_users_workout_is_404(self, client: TestClient, db: Session, garmin_247: Garmin247Data) -> None:
        owner = UserFactory()
        workout = _garmin_workout(db, garmin_247, owner)

        assert self._get(client, UserFactory().id, workout.id).status_code == 404
        assert self._get(client, owner.id, uuid4()).status_code == 404

    def test_invalid_params_are_400(self, client: TestClient, db: Session, garmin_247: Garmin247Data) -> None:
        user = UserFactory()
        workout = _garmin_workout(db, garmin_247, user)

        assert self._get(client, user.id, workout.id, types=["steps"]).status_code == 400
        # the app maps request validation errors to 400
        assert self._get(client, user.id, workout.id, max_points=2001).status_code == 400

    def test_requires_api_key(self, client: TestClient, db: Session) -> None:
        response = client.get(f"/api/v1/users/{uuid4()}/events/workouts/{uuid4()}/samples")
        assert response.status_code == 401


class TestWorkoutPace:
    def test_list_derives_pace_from_distance_and_duration(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        workout = EventRecordFactory(
            data_source=source,
            type="running",
            duration_seconds=3000,
            start_datetime=START,
            end_datetime=START + timedelta(seconds=3000),
        )
        WorkoutDetailsFactory(event_record=workout, distance=Decimal("10000"))
        yoga = EventRecordFactory(
            data_source=source,
            type="yoga",
            duration_seconds=1800,
            start_datetime=START + timedelta(hours=2),
            end_datetime=START + timedelta(hours=2, seconds=1800),
        )
        WorkoutDetailsFactory(event_record=yoga, distance=Decimal("100"))

        response = client.get(
            f"/api/v1/users/{user.id}/events/workouts",
            headers=api_key_headers(ApiKeyFactory().id),
            params={"start_date": "2025-11-19", "end_date": "2025-11-21"},
        )

        assert response.status_code == 200
        by_type = {w["type"]: w for w in response.json()["data"]}
        assert by_type["running"]["avg_pace_sec_per_km"] == 300.0  # 3000 s over 10 km
        assert by_type["yoga"]["avg_pace_sec_per_km"] is None
