"""GPS samples keep 7-decimal precision end to end and legacy degree rows stay readable."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataPointSeries, EventRecord
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from tests.factories import ApiKeyFactory, DataSourceFactory, EventRecordFactory, UserFactory
from tests.utils import api_key_headers

START = datetime(2025, 11, 20, 7, 0, tzinfo=timezone.utc)
START_TS = int(START.timestamp())
SECONDS = 60
TOLERANCE = 1e-6


def _lat(i: int) -> float:
    return round(52.2296756 + i * 0.0000137, 7)


def _lon(i: int) -> float:
    return round(-121.0122287 - i * 0.0000211, 7)  # negative longitude on purpose


@pytest.fixture
def garmin_247() -> Garmin247Data:
    oauth = GarminOAuth(
        user_repo=MagicMock(),
        connection_repo=UserConnectionRepository(),
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
    )
    return Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=oauth)


def _workout(user: Any) -> EventRecord:
    data_source = DataSourceFactory(
        user=user, provider=ProviderName.GARMIN, source="garmin", device_model="Forerunner 965", device_type=None
    )
    return EventRecordFactory(
        data_source=data_source,
        category="workout",
        type="running",
        duration_seconds=SECONDS,
        start_datetime=START,
        end_datetime=START + timedelta(seconds=SECONDS),
    )


def _push_route(db: Session, garmin_247: Garmin247Data, user: Any) -> None:
    """activityDetails samples with 7-decimal coordinates, through the real sample builder."""
    item = {
        "summary": {"activityId": 1, "startTimeInSeconds": START_TS, "startTimeOffsetInSeconds": 0},
        "samples": [
            {"startTimeInSeconds": START_TS + i, "latitudeInDegree": _lat(i), "longitudeInDegree": _lon(i)}
            for i in range(SECONDS)
        ],
    }
    samples = garmin_247._build_activity_samples(user.id, item)
    assert {s.series_type for s in samples} == {SeriesType.latitude_e4, SeriesType.longitude_e4}
    DataPointSeriesRepository(DataPointSeries).bulk_create(db, samples)
    db.flush()


def _legacy_rows(db: Session, user: Any, seconds: range, lat: float, lon: float) -> None:
    """Rows in the pre-scaling format: plain degrees under latitude/longitude (3 decimals kept)."""
    samples = [
        TimeSeriesSampleCreate(
            id=uuid4(),
            user_id=user.id,
            source="garmin",
            recorded_at=START + timedelta(seconds=i),
            value=Decimal(str(value)),
            series_type=series_type,
        )
        for i in seconds
        for series_type, value in ((SeriesType.latitude, lat), (SeriesType.longitude, lon))
    ]
    DataPointSeriesRepository(DataPointSeries).bulk_create(db, samples)
    db.flush()


def _route(client: TestClient, user: Any, workout: EventRecord) -> list[list[Any]]:
    response = client.get(
        f"/api/v1/users/{user.id}/events/workouts/{workout.id}/samples",
        headers=api_key_headers(ApiKeyFactory().id),
        params={"types": ["latitude", "longitude"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["route"]


def _timeseries(client: TestClient, user: Any, types: list[str] | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "start_time": START.isoformat(),
        "end_time": (START + timedelta(seconds=SECONDS)).isoformat(),
        "limit": 100,
    }
    if types is not None:
        params["types"] = types
    rows: list[dict[str, Any]] = []
    while True:
        response = client.get(
            f"/api/v1/users/{user.id}/timeseries", headers=api_key_headers(ApiKeyFactory().id), params=params
        )
        assert response.status_code == 200, response.text
        body = response.json()
        rows.extend(body["data"])
        if not body["pagination"]["has_more"]:
            return rows
        params["cursor"] = body["pagination"]["next_cursor"]


class TestScaledGps:
    def test_samples_endpoint_route_keeps_seven_decimals(
        self, client: TestClient, db: Session, garmin_247: Garmin247Data
    ) -> None:
        user = UserFactory()
        workout = _workout(user)
        _push_route(db, garmin_247, user)

        route = _route(client, user, workout)

        assert len(route) == SECONDS
        for ts, lat, lon in route:
            i = int((datetime.fromisoformat(ts) - START).total_seconds())
            assert abs(lat - _lat(i)) < TOLERANCE
            assert abs(lon - _lon(i)) < TOLERANCE

    def test_timeseries_returns_degrees_never_scaled(
        self, client: TestClient, db: Session, garmin_247: Garmin247Data
    ) -> None:
        user = UserFactory()
        _push_route(db, garmin_247, user)

        rows = _timeseries(client, user, ["latitude", "longitude"])

        assert len(rows) == 2 * SECONDS
        assert {r["type"] for r in rows} == {"latitude", "longitude"}
        assert {r["unit"] for r in rows} == {"degrees"}
        for r in rows:
            i = int((datetime.fromisoformat(r["timestamp"]) - START).total_seconds())
            expected = _lat(i) if r["type"] == "latitude" else _lon(i)
            assert abs(r["value"] - expected) < TOLERANCE

        # Unfiltered reads never surface the storage types or scaled values either.
        everything = _timeseries(client, user)
        assert {r["type"] for r in everything} == {"latitude", "longitude"}
        assert all(abs(r["value"]) <= 180 for r in everything)

    def test_legacy_rows_still_read(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        workout = _workout(user)
        _legacy_rows(db, user, range(0, SECONDS, 10), lat=52.229, lon=-121.012)

        route = _route(client, user, workout)
        assert len(route) == 6
        assert all(lat == 52.229 and lon == -121.012 for _, lat, lon in route)

        rows = _timeseries(client, user, ["latitude"])
        assert [r["value"] for r in rows] == [52.229] * 6
        assert {r["unit"] for r in rows} == {"degrees"}

    def test_mixed_window_prefers_scaled_rows(self, client: TestClient, db: Session, garmin_247: Garmin247Data) -> None:
        """The same samples re-ingested after the switch: the scaled copy wins, the legacy one is hidden."""
        user = UserFactory()
        workout = _workout(user)
        _legacy_rows(db, user, range(SECONDS), lat=10.0, lon=10.0)  # sentinel values, must not appear
        _push_route(db, garmin_247, user)

        route = _route(client, user, workout)
        assert len(route) == SECONDS
        assert all(abs(lat - 10.0) > 1 for _, lat, _ in route)

        rows = _timeseries(client, user, ["latitude", "longitude"])
        assert len(rows) == 2 * SECONDS
        assert all(abs(r["value"] - 10.0) > 1 for r in rows)
