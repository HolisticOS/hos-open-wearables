"""The member-local days an SDK batch touched, which the HOS notify carries as `dates`."""

from typing import Any
from unittest.mock import MagicMock

from app.schemas.providers.mobile_sdk import SyncRequest
from app.services.sdk.import_service import batch_local_dates


def _request(
    records: list[dict[str, Any]] | None = None,
    workouts: list[dict[str, Any]] | None = None,
    sleep: list[dict[str, Any]] | None = None,
) -> SyncRequest:
    return SyncRequest.model_validate(
        {
            "provider": "health_connect",
            "sdkVersion": "0.10.0",
            "syncTimestamp": "2026-11-16T08:00:00Z",
            "data": {"records": records or [], "workouts": workouts or [], "sleep": sleep or []},
        }
    )


def _hr(start: str, offset: str | None) -> dict[str, Any]:
    return {
        "type": "HEART_RATE",
        "startDate": start,
        "endDate": start,
        "zoneOffset": offset,
        "value": 60,
        "unit": "bpm",
    }


def _no_member_offset() -> str | None:
    raise AssertionError("the member's offset is only looked up when the batch has none")


class TestBatchLocalDates:
    def test_records_fall_on_their_local_day(self) -> None:
        # 23:30 UTC is the next morning in UTC+8, still the same evening in UTC-5
        request = _request(records=[_hr("2026-11-14T23:30:00Z", "+08:00"), _hr("2026-11-14T23:30:00Z", "-05:00")])
        assert batch_local_dates(request, _no_member_offset) == ["2026-11-14", "2026-11-15"]

    def test_deduped_and_sorted(self) -> None:
        request = _request(
            records=[
                _hr("2026-11-15T10:00:00Z", "+00:00"),
                _hr("2026-11-13T10:00:00Z", "+00:00"),
                _hr("2026-11-15T11:00:00Z", "+00:00"),
            ]
        )
        assert batch_local_dates(request, _no_member_offset) == ["2026-11-13", "2026-11-15"]

    def test_a_workout_over_midnight_touches_both_days(self) -> None:
        workout = {"type": "running", "startDate": "2026-11-14T23:30:00Z", "endDate": "2026-11-15T00:30:00Z"}
        workout["zoneOffset"] = "+00:00"
        assert batch_local_dates(_request(workouts=[workout]), _no_member_offset) == ["2026-11-14", "2026-11-15"]

    def test_sleep_counts_on_its_wake_day_only(self) -> None:
        stage = {"stage": "light", "startDate": "2026-11-14T22:00:00Z", "endDate": "2026-11-15T06:00:00Z"}
        stage["zoneOffset"] = "+00:00"
        assert batch_local_dates(_request(sleep=[stage]), _no_member_offset) == ["2026-11-15"]

    def test_java_utc_spelling_is_utc(self) -> None:
        assert batch_local_dates(_request(records=[_hr("2026-11-14T23:30:00Z", "Z")]), _no_member_offset) == [
            "2026-11-14"
        ]

    def test_a_record_without_an_offset_takes_the_batchs(self) -> None:
        request = _request(records=[_hr("2026-11-14T23:30:00Z", None), _hr("2026-11-14T20:00:00Z", "+08:00")])
        assert batch_local_dates(request, _no_member_offset) == ["2026-11-15"]

    def test_a_batch_without_offsets_takes_the_members_latest(self) -> None:
        member_offset = MagicMock(return_value="+08:00")
        request = _request(records=[_hr("2026-11-14T23:30:00Z", None)])
        assert batch_local_dates(request, member_offset) == ["2026-11-15"]
        member_offset.assert_called_once()

    def test_without_any_offset_it_is_utc(self) -> None:
        request = _request(records=[_hr("2026-11-14T23:30:00Z", None)])
        assert batch_local_dates(request, lambda: None) == ["2026-11-14"]

    def test_an_empty_batch_has_no_days(self) -> None:
        assert batch_local_dates(_request(), _no_member_offset) == []

    def test_a_mistaken_long_span_adds_its_ends_only(self) -> None:
        record = {"type": "STEP_COUNT", "startDate": "2026-01-01T12:00:00Z", "endDate": "2026-11-15T12:00:00Z"}
        record |= {"zoneOffset": "+00:00", "value": 10, "unit": "count"}
        assert batch_local_dates(_request(records=[record]), _no_member_offset) == ["2026-01-01", "2026-11-15"]
