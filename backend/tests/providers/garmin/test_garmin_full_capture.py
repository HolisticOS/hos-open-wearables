"""Garmin push fields that are stored and readable back through the external API.

Payload values are taken from the sample Garmin payloads already used in
``test_garmin_247.py`` and ``tests/tasks/test_garmin_webhook_task.py``.
"""

from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants.webhooks.events import SERIES_TYPE_TO_GRANULAR_EVENT, SERIES_TYPE_TO_GROUP_EVENT
from app.integrations.celery.tasks.webhook_push_task import process_webhook_push
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import AggregationMethod, SeriesType, get_series_type_id, get_series_type_unit
from app.schemas.enums.aggregation_method import get_aggregation_method
from app.schemas.providers.garmin import ActivityJSON
from app.services.providers.garmin.coverage import TIMESERIES
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.workouts import GarminWorkouts
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers

MODULE = "app.integrations.celery.tasks.webhook_push_task"

GARMIN_USER_ID = "garmin_user_capture"
DAY_START = 1705276800  # 2024-01-15 00:00:00 UTC


@pytest.fixture
def garmin_247() -> Garmin247Data:
    oauth = GarminOAuth(
        user_repo=MagicMock(),
        connection_repo=UserConnectionRepository(),
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
    )
    return Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=oauth)


@pytest.fixture
def sample_daily() -> dict[str, Any]:
    return {
        "userId": GARMIN_USER_ID,
        "summaryId": "daily_123",
        "calendarDate": "2024-01-15",
        "startTimeInSeconds": DAY_START,
        "startTimeOffsetInSeconds": 3600,
        "durationInSeconds": 86400,
        "steps": 12500,
        "distanceInMeters": 9500.5,
        "activeKilocalories": 650,
        "bmrKilocalories": 1800,
        "floorsClimbed": 12,
        "restingHeartRateInBeatsPerMinute": 55,
        "averageStressLevel": 35,
        "maxStressLevel": 88,
        "restStressDurationInSeconds": 28800,
        "lowStressDurationInSeconds": 14400,
        "mediumStressDurationInSeconds": 3600,
        "highStressDurationInSeconds": 900,
        "stressQualifier": "balanced",
        "moderateIntensityDurationInSeconds": 1500,
        "vigorousIntensityDurationInSeconds": 720,
    }


def _by_type(samples: list[Any]) -> dict[SeriesType, list[Any]]:
    grouped: dict[SeriesType, list[Any]] = {}
    for s in samples:
        grouped.setdefault(s.series_type, []).append(s)
    return grouped


class TestNewSeriesTypes:
    @pytest.mark.parametrize(
        ("series_type", "type_id"),
        [(SeriesType.intensity_minutes_moderate, 89), (SeriesType.intensity_minutes_vigorous, 90)],
    )
    def test_registered(self, series_type: SeriesType, type_id: int) -> None:
        assert get_series_type_id(series_type) == type_id
        assert get_series_type_unit(series_type) == "minutes"
        assert get_aggregation_method(series_type) is AggregationMethod.SUM
        assert len(series_type.value) <= 32  # series_type_definition.code is VARCHAR(32)
        assert series_type.value in SERIES_TYPE_TO_GROUP_EVENT
        assert series_type.value in SERIES_TYPE_TO_GRANULAR_EVENT
        assert series_type in TIMESERIES


class TestDailiesCapture:
    def test_bmr_and_intensity_minutes_are_daily_totals(
        self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user_id = uuid4()
        normalized, _ = garmin_247.normalize_dailies(sample_daily, user_id)
        by_type = _by_type(garmin_247._build_dailies_samples(user_id, normalized))

        basal = by_type[SeriesType.basal_energy][0]
        assert basal.value == Decimal("1800")
        assert basal.is_daily_total is True

        moderate = by_type[SeriesType.intensity_minutes_moderate][0]
        vigorous = by_type[SeriesType.intensity_minutes_vigorous][0]
        assert moderate.value == Decimal("25")  # 1500 s
        assert vigorous.value == Decimal("12")  # 720 s
        assert moderate.is_daily_total is True
        assert moderate.recorded_at == datetime(2024, 1, 15, tzinfo=timezone.utc)
        assert moderate.zone_offset == "+01:00"

    def test_absent_intensity_fields_are_not_stored_as_zero(
        self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        user_id = uuid4()
        intensity_keys = ("moderateIntensityDurationInSeconds", "vigorousIntensityDurationInSeconds")
        daily = {k: v for k, v in sample_daily.items() if k not in intensity_keys}
        normalized, _ = garmin_247.normalize_dailies(daily, user_id)
        by_type = _by_type(garmin_247._build_dailies_samples(user_id, normalized))

        assert SeriesType.intensity_minutes_moderate not in by_type
        assert SeriesType.intensity_minutes_vigorous not in by_type

    def test_zero_intensity_is_stored(self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]) -> None:
        user_id = uuid4()
        daily = {**sample_daily, "vigorousIntensityDurationInSeconds": 0}
        normalized, _ = garmin_247.normalize_dailies(daily, user_id)
        by_type = _by_type(garmin_247._build_dailies_samples(user_id, normalized))

        assert by_type[SeriesType.intensity_minutes_vigorous][0].value == Decimal("0")

    def test_stress_score_carries_max_and_durations(
        self, garmin_247: Garmin247Data, sample_daily: dict[str, Any]
    ) -> None:
        _, scores = garmin_247.normalize_dailies(sample_daily, uuid4())

        assert len(scores) == 1
        score = scores[0]
        assert score.value == 35
        assert score.qualifier == "Balanced"
        assert score.zone_offset == "+01:00"
        assert score.components is not None
        assert score.components["max_stress_level"].value == 88
        assert score.components["rest_stress_duration_seconds"].value == 28800
        assert score.components["high_stress_duration_seconds"].value == 900
        assert "activity_stress_duration_seconds" not in score.components  # absent in payload


class TestEpochsCapture:
    def test_distance_active_time_and_met(self, garmin_247: Garmin247Data) -> None:
        user_id = uuid4()
        epoch = {
            "summaryId": "x1-65a4f710-3",
            "startTimeInSeconds": 1705309200,
            "durationInSeconds": 900,
            "activeTimeInSeconds": 540,
            "steps": 250,
            "distanceInMeters": 200.5,
            "activeKilocalories": 15,
            "met": 3.2,
            "meanHeartRateInBeatsPerMinute": 85,
            "intensity": "ACTIVE",
        }
        normalized = garmin_247.normalize_epochs([epoch], user_id)
        by_type = _by_type(garmin_247._build_epochs_samples(user_id, normalized))

        assert by_type[SeriesType.distance_walking_running][0].value == Decimal("200.5")
        assert by_type[SeriesType.distance_walking_running][0].is_daily_total is False
        assert by_type[SeriesType.active_time][0].value == Decimal("9")  # 540 s
        assert by_type[SeriesType.active_time][0].is_daily_total is False
        assert by_type[SeriesType.average_met][0].value == Decimal("3.2")
        assert by_type[SeriesType.average_met][0].is_daily_total is None
        assert by_type[SeriesType.distance_walking_running][0].external_id == "x1-65a4f710"

    def test_epochs_of_one_slot_are_merged(self, garmin_247: Garmin247Data) -> None:
        """One epoch per activity type shares the slot start; sums add up, rates average."""
        user_id = uuid4()
        slot = {"startTimeInSeconds": 1705309200, "durationInSeconds": 900}
        epochs = [
            {
                **slot,
                "summaryId": "x1-65a4f710-0",
                "steps": 40,
                "activeKilocalories": 2,
                "meanHeartRateInBeatsPerMinute": 70,
            },
            {
                **slot,
                "summaryId": "x1-65a4f710-3",
                "steps": 210,
                "activeKilocalories": 13,
                "meanHeartRateInBeatsPerMinute": 90,
            },
        ]
        normalized = garmin_247.normalize_epochs(epochs, user_id)
        by_type = _by_type(garmin_247._build_epochs_samples(user_id, normalized))

        assert len(by_type[SeriesType.steps]) == 1
        assert by_type[SeriesType.steps][0].value == Decimal("250")
        assert by_type[SeriesType.energy][0].value == Decimal("15")
        assert by_type[SeriesType.heart_rate][0].value == Decimal("80")

    @patch("app.repositories.data_point_series_repository.DataPointSeriesRepository.bulk_create")
    def test_batch_merges_slot_across_items(
        self, mock_bulk_create: MagicMock, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        slot = {"startTimeInSeconds": 1705309200, "durationInSeconds": 900}
        items = [
            {**slot, "summaryId": "x1-65a4f710-0", "steps": 40},
            {**slot, "summaryId": "x1-65a4f710-3", "steps": 210},
        ]
        mock_bulk_create.return_value = 1

        garmin_247.process_items_batch(db, user.id, "epochs", items)

        samples = mock_bulk_create.call_args[0][1]
        assert [s.value for s in samples if s.series_type == SeriesType.steps] == [Decimal("250")]


class TestBloodPressurePulse:
    def test_pulse_is_heart_rate_reading(self, garmin_247: Garmin247Data) -> None:
        bp = {
            "summaryId": "sd60a3665-6a31b950",
            "systolic": 112,
            "diastolic": 73,
            "pulse": 69,
            "sourceType": "MANUAL",
            "measurementTimeInSeconds": 1781643600,
            "measurementTimeOffsetInSeconds": -18000,
        }
        by_type = _by_type(garmin_247._build_blood_pressure_samples(uuid4(), bp))

        pulse = by_type[SeriesType.heart_rate][0]
        assert pulse.value == Decimal("69")
        assert pulse.recorded_at == datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
        assert pulse.external_id == "sd60a3665-6a31b950:pulse"


class TestActivityRecords:
    def test_activity_details_use_unified_workout_type_and_steps(self, garmin_247: Garmin247Data) -> None:
        summary = {
            "activityId": 21047282990,
            "activityType": "TREADMILL_RUNNING",
            "deviceName": "Forerunner 965",
            "startTimeInSeconds": 1763597760,
            "durationInSeconds": 3600,
            "steps": 9800,
        }
        result = garmin_247._build_activity_record(uuid4(), summary)

        assert result is not None
        record, detail = result
        assert record.type == "treadmill"  # same mapping as the activities push
        assert record.device_model == "Forerunner 965"
        assert detail.steps_count == 9800

    def test_activities_push_maps_speed_and_elevation(self) -> None:
        workouts = GarminWorkouts(
            workout_repo=MagicMock(),
            connection_repo=MagicMock(),
            provider_name="garmin",
            api_base_url="https://apis.garmin.com",
            oauth=MagicMock(),
        )
        activity = ActivityJSON(
            userId=GARMIN_USER_ID,
            activityId=21047282990,
            activityType="RUNNING",
            startTimeInSeconds=1763597760,
            durationInSeconds=3600,
            deviceName="Forerunner 965",
            averageSpeedInMetersPerSecond=2.78,
            elevationGainInMeters=48.0,
        )
        record, detail = workouts._normalize_workout(activity, uuid4())

        assert detail.average_speed == Decimal("2.78")
        assert detail.total_elevation_gain == Decimal("48.0")
        assert record.device_model == "Forerunner 965"


@pytest.fixture
def task_db(db: Session) -> Generator[Session, None, None]:
    """Run the webhook task on the test session (same pattern as test_garmin_webhook_task)."""
    with (
        patch(f"{MODULE}.SessionLocal", return_value=db),
        patch(f"{MODULE}.ProviderFactory") as mock_factory_cls,
        patch.object(db, "close", MagicMock()),
    ):
        from app.services.providers.garmin.strategy import GarminStrategy

        mock_factory_cls.return_value.get_provider.return_value = GarminStrategy()
        yield db


class TestPushToReadEndpoints:
    """A push payload is readable back through the endpoints a server-side consumer uses."""

    def _get(self, client: TestClient, api_key: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = client.get(path, headers=api_key_headers(api_key), params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def test_push_then_read(
        self,
        task_db: Session,
        client: TestClient,
        mock_external_apis: dict[str, MagicMock],
        sample_daily: dict[str, Any],
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", provider_user_id=GARMIN_USER_ID)
        api_key = ApiKeyFactory()
        sleep_start = DAY_START - 7200  # 2024-01-14 22:00 UTC
        payload = {
            "dailies": [sample_daily],
            "stressDetails": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": "stress-1",
                    "startTimeInSeconds": DAY_START,
                    "durationInSeconds": 86400,
                    "timeOffsetStressLevelValues": {"0": 25, "180": 30, "360": -1},
                    "timeOffsetBodyBatteryValues": {"0": 80, "180": 78},
                }
            ],
            "allDayRespiration": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": "resp-1",
                    "startTimeInSeconds": DAY_START,
                    "timeOffsetEpochToBreaths": {"0": 11.33, "60": 15.36},
                }
            ],
            "pulseox": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": "spo2-1",
                    "startTimeInSeconds": DAY_START,
                    "durationInSeconds": 3600,
                    "timeOffsetSpo2Values": {"0": 98, "300": 97},
                }
            ],
            "userMetrics": [
                {"userId": GARMIN_USER_ID, "summaryId": "um-1", "calendarDate": "2024-01-15", "vo2Max": 48.0}
            ],
            "sleeps": [
                {
                    "userId": GARMIN_USER_ID,
                    "summaryId": "sleep-1",
                    "calendarDate": "2024-01-15",
                    "startTimeInSeconds": sleep_start,
                    "durationInSeconds": 5400,
                    "deepSleepDurationInSeconds": 1800,
                    "lightSleepDurationInSeconds": 1800,
                    "remSleepInSeconds": 1800,
                    "awakeDurationInSeconds": 600,
                    "sleepLevelsMap": {
                        "light": [{"startTimeInSeconds": sleep_start, "endTimeInSeconds": sleep_start + 1800}],
                        "deep": [{"startTimeInSeconds": sleep_start + 1800, "endTimeInSeconds": sleep_start + 3600}],
                        "rem": [{"startTimeInSeconds": sleep_start + 3600, "endTimeInSeconds": sleep_start + 5400}],
                        "awake": [{"startTimeInSeconds": sleep_start + 5400, "endTimeInSeconds": sleep_start + 6000}],
                    },
                    "overallSleepScore": {"value": 82, "qualifier": "GOOD"},
                }
            ],
        }

        result = process_webhook_push.run("garmin", payload, "trace-capture")
        assert result["errors"] == []

        base = f"/api/v1/users/{user.id}"
        window = {"start_time": "2024-01-14T00:00:00Z", "end_time": "2024-01-16T00:00:00Z"}

        def values(series: str) -> list[float]:
            body = self._get(client, api_key.id, f"{base}/timeseries", {**window, "types": series, "limit": 100})
            return [s["value"] for s in body["data"]]

        assert values("garmin_body_battery") == [80, 78]
        assert values("garmin_stress_level") == [25, 30]
        assert values("respiratory_rate") == [11.33, 15.36]
        assert values("oxygen_saturation") == [98, 97]
        assert values("vo2_max") == [48.0]
        assert values("intensity_minutes_moderate") == [25]
        assert values("intensity_minutes_vigorous") == [12]
        assert values("basal_energy") == [1800]

        moderate = self._get(
            client, api_key.id, f"{base}/timeseries", {**window, "types": "intensity_minutes_moderate"}
        )["data"][0]
        assert moderate["unit"] == "minutes"
        assert moderate["is_daily_total"] is True
        assert moderate["source"]["provider"] == "garmin"

        sleep = self._get(
            client,
            api_key.id,
            f"{base}/events/sleep",
            {"start_date": "2024-01-14T00:00:00Z", "end_date": "2024-01-16T00:00:00Z"},
        )["data"]
        assert len(sleep) == 1
        assert [i["stage"] for i in sleep[0]["sleep_stage_intervals"]] == ["light", "deep", "rem", "awake"]
        assert sleep[0]["stages"]["deep_minutes"] == 30

        scores = self._get(
            client,
            api_key.id,
            f"{base}/health-scores",
            {"start_date": "2024-01-14T00:00:00Z", "end_date": "2024-01-16T00:00:00Z", "provider": "garmin"},
        )["data"]
        by_category = {s["category"]: s for s in scores}
        assert by_category["sleep"]["value"] == 82
        assert by_category["body_battery"]["value"] == 80
        assert by_category["stress"]["value"] == 35
        assert by_category["stress"]["components"]["max_stress_level"]["value"] == 88
