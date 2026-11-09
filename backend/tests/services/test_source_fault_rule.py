"""Tests for the device-fault override. The cases are real days from the backtest."""

from datetime import date

from app.services.source_fault_rule import (
    apply_activity_fault_rule,
    apply_sleep_fault_rule,
    is_mirror,
    is_wearable,
    rule_applies,
)


def garmin(steps: int, hr_hours: int | None, **extra: object) -> dict:
    entry = {"provider": "garmin", "source": "garmin", "device_model": None, "steps_sum": steps, "hr_hours": hr_hours}
    return entry | extra


def iphone(steps: int, **extra: object) -> dict:
    entry = {"provider": "apple", "source": "iPhone", "device_model": "iPhone16,2", "steps_sum": steps, "hr_hours": 0}
    return entry | extra


def connect(steps: int) -> dict:
    return {"provider": "apple", "source": "Connect", "device_model": None, "steps_sum": steps, "hr_hours": 20}


def picked(leader: dict, *others: dict) -> int:
    entries = [leader, *others]
    return apply_activity_fault_rule(entries, leader)["steps_sum"]


def test_barely_worn_watch_loses_to_the_phone() -> None:
    assert picked(garmin(826, 4), iphone(10732)) == 10732


def test_partly_worn_watch_loses_when_the_phone_clearly_saw_more() -> None:
    assert picked(garmin(2686, 10), iphone(7371)) == 7371


def test_worn_watch_keeps_its_number_even_when_the_phone_is_far_higher() -> None:
    assert picked(garmin(10284, 20), iphone(23830)) == 10284


def test_worn_watch_above_the_phone_keeps_its_number() -> None:
    assert picked(garmin(8918, 24), iphone(2265)) == 8918


def test_small_gaps_do_not_switch() -> None:
    assert picked(garmin(1000, 4), iphone(1400)) == 1000  # under 1.5x
    assert picked(garmin(100, 4), iphone(900)) == 100  # under 1,000 more


def test_the_watch_copied_into_apple_health_is_not_a_second_opinion() -> None:
    assert picked(garmin(826, 4), connect(10000)) == 826


def test_a_missing_wear_signal_changes_nothing() -> None:
    assert picked(garmin(826, None), iphone(10732)) == 826


def test_a_phone_leader_is_not_second_guessed() -> None:
    assert picked(iphone(500), {"provider": "apple", "source": "Pacer", "device_model": None, "steps_sum": 9000}) == 500


def test_every_metric_is_compared_on_its_own() -> None:
    leader = garmin(826, 4, active_energy_sum=50.0, distance_sum=600.0, flights_climbed_sum=8, active_time_minutes=5)
    donor = iphone(10732, active_energy_sum=400.0, distance_sum=8000.0, flights_climbed_sum=9, active_time_minutes=90)

    result = apply_activity_fault_rule([leader, donor], leader)

    assert result["steps_sum"] == 10732
    assert result["active_energy_sum"] == 400.0
    assert result["distance_sum"] == 8000.0
    assert result["active_time_minutes"] == 90
    assert result["flights_climbed_sum"] == 8  # the phone is not clearly ahead
    assert result["source"] == "garmin"
    assert leader["steps_sum"] == 826  # the input row is left alone


def test_heart_rate_moves_whole_to_the_source_that_covered_more_of_the_day() -> None:
    leader = garmin(5000, 4, hr_avg=60, hr_max=90, hr_min=50)
    watch = {
        "provider": "apple",
        "source": "Apple Watch",
        "device_model": "Watch7,1",
        "steps_sum": 5200,
        "hr_hours": 18,
        "hr_avg": 72,
        "hr_max": 150,
        "hr_min": 48,
    }

    result = apply_activity_fault_rule([leader, watch], leader)

    assert (result["hr_avg"], result["hr_max"], result["hr_min"]) == (72, 150, 48)


def night(provider: str, source: str, minutes: int, model: str | None = None) -> dict:
    return {"provider": provider, "source": source, "device_model": model, "total_duration_minutes": minutes}


def test_a_watch_that_died_overnight_loses_the_night() -> None:
    leader, donor = night("garmin", "garmin", 120), night("apple", "iPhone", 430, "iPhone16,2")
    assert apply_sleep_fault_rule([leader, donor], leader) is donor


def test_a_normal_night_stays_with_the_watch() -> None:
    leader, donor = night("garmin", "garmin", 368), night("apple", "iPhone", 383, "iPhone16,2")
    assert apply_sleep_fault_rule([leader, donor], leader) is leader


def test_a_partial_phone_reading_never_wins_a_night() -> None:
    leader, donor = night("garmin", "garmin", 40), night("apple", "iPhone", 86, "iPhone16,2")
    assert apply_sleep_fault_rule([leader, donor], leader) is leader


def test_the_mirror_cannot_win_a_night_either() -> None:
    leader, donor = night("garmin", "garmin", 120), night("apple", "Connect", 430)
    assert apply_sleep_fault_rule([leader, donor], leader) is leader


def test_device_classification() -> None:
    assert is_wearable(garmin(1, 1))
    assert is_wearable({"provider": "apple", "device_model": "Watch7,1"})
    assert not is_wearable(iphone(1))
    assert is_mirror(garmin(1, 1), connect(1))
    assert not is_mirror(garmin(1, 1), iphone(1))


def test_the_rule_only_starts_on_its_date() -> None:
    start = date(2026, 11, 10)
    assert rule_applies(date(2026, 11, 10), start)
    assert not rule_applies(date(2026, 11, 9), start)
    assert not rule_applies(date(2026, 11, 10), None)
