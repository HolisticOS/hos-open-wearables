"""A connected wearable is preferred; Apple Health fills only what the wearable lacks."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.schemas.enums import DEFAULT_PROVIDER_PRIORITY, ProviderName, SeriesType
from app.services.event_record_service import EventRecordService
from app.services.summaries_service import ACTIVITY_FALLBACK_GROUPS, SummariesService

T0 = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)


class TestActivityFallback:
    def test_wearable_row_keeps_its_values_and_borrows_only_missing_groups(self) -> None:
        garmin = {
            "provider": "garmin",
            "source": "garmin",
            "steps_sum": 9000,
            "hr_avg": None,
            "hr_max": None,
            "hr_min": None,
            "distance_sum": 7000.0,
        }
        apple = {
            "provider": "apple",
            "source": "Apple Watch",
            "steps_sum": 8000,
            "hr_avg": 70,
            "hr_max": 150,
            "hr_min": 50,
            "distance_sum": 6500.0,
        }

        merged = SummariesService._fill_from_fallbacks([garmin, apple], ACTIVITY_FALLBACK_GROUPS)

        assert merged["source"] == "garmin"
        assert merged["steps_sum"] == 9000
        assert merged["distance_sum"] == 7000.0
        assert (merged["hr_avg"], merged["hr_max"], merged["hr_min"]) == (70, 150, 50)
        assert garmin["hr_avg"] is None  # the input row isn't mutated

    def test_zero_sum_counts_as_missing(self) -> None:
        garmin = {"provider": "garmin", "steps_sum": 0}
        apple = {"provider": "apple", "steps_sum": 4200}

        merged = SummariesService._fill_from_fallbacks([garmin, apple], ACTIVITY_FALLBACK_GROUPS)

        assert merged["steps_sum"] == 4200

    def test_without_groups_the_winner_is_returned_untouched(self) -> None:
        garmin = {"provider": "garmin", "hr_avg": None}
        apple = {"provider": "apple", "hr_avg": 70}

        assert SummariesService._fill_from_fallbacks([garmin, apple], ()) is garmin


class TestSleepVitalsPreferNightSource:
    def test_night_source_first_then_by_priority(self) -> None:
        by_provider = {
            "apple": {SeriesType.heart_rate: 58.0, SeriesType.oxygen_saturation: 96.0},
            "garmin": {SeriesType.heart_rate: 52.0},
        }

        merged = SummariesService._prefer_provider(by_provider, "garmin", DEFAULT_PROVIDER_PRIORITY)

        assert merged[SeriesType.heart_rate] == 52.0
        assert merged[SeriesType.oxygen_saturation] == 96.0


def _workout(provider: str, start_min: int, end_min: int) -> tuple[SimpleNamespace, SimpleNamespace]:
    record = SimpleNamespace(
        start_datetime=T0 + timedelta(minutes=start_min), end_datetime=T0 + timedelta(minutes=end_min)
    )
    return record, SimpleNamespace(provider=provider)


class TestWorkoutDedupe:
    def test_apple_copy_of_a_garmin_session_is_dropped(self) -> None:
        garmin = _workout(ProviderName.GARMIN.value, 0, 45)
        apple_copy = _workout(ProviderName.APPLE.value, 0, 44)

        kept = EventRecordService._drop_duplicate_workouts([garmin, apple_copy], DEFAULT_PROVIDER_PRIORITY)

        assert kept == [garmin]

    def test_separate_sessions_and_same_provider_are_kept(self) -> None:
        garmin_run = _workout(ProviderName.GARMIN.value, 0, 45)
        apple_walk = _workout(ProviderName.APPLE.value, 40, 100)  # 5 min overlap of a 45 min session
        garmin_again = _workout(ProviderName.GARMIN.value, 10, 30)

        kept = EventRecordService._drop_duplicate_workouts(
            [garmin_run, apple_walk, garmin_again], DEFAULT_PROVIDER_PRIORITY
        )

        assert kept == [garmin_run, apple_walk, garmin_again]
