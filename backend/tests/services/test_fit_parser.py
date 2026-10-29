from decimal import Decimal
from uuid import uuid4

import fitdecode
import pytest

from app.schemas.enums.series_types import SeriesType
from app.services.fit_parser import FitParseResult, parse_fit_file
from app.services.workout_samples_service import WorkoutSamplesService
from tests.fixtures.fit_builder import make_cycling_fit, make_running_fit, make_strength_fit, make_swimming_fit

USER_ID = uuid4()
DS_ID = uuid4()


@pytest.fixture(scope="module")
def running() -> FitParseResult:
    return parse_fit_file(make_running_fit(), USER_ID, DS_ID, source="garmin")


@pytest.fixture(scope="module")
def cycling() -> FitParseResult:
    return parse_fit_file(make_cycling_fit(), USER_ID, DS_ID, source="garmin")


@pytest.fixture(scope="module")
def swimming() -> FitParseResult:
    return parse_fit_file(make_swimming_fit(), USER_ID, DS_ID, source="garmin")


class TestRunning:
    def test_sample_count(self, running: FitParseResult) -> None:
        assert len(running.samples) > 0

    def test_expected_series_types(self, running: FitParseResult) -> None:
        types = {s.series_type for s in running.samples}
        assert SeriesType.heart_rate in types
        assert SeriesType.speed in types
        assert SeriesType.cadence in types
        assert SeriesType.power in types
        assert SeriesType.running_vertical_oscillation in types
        assert SeriesType.running_ground_contact_time in types
        # GPS is written to the scaled storage types (degrees x 10,000)
        assert SeriesType.latitude_e4 in types
        assert SeriesType.longitude_e4 in types
        assert SeriesType.latitude not in types
        assert SeriesType.elevation in types
        assert SeriesType.air_temperature in types
        assert SeriesType.running_vertical_ratio in types
        assert SeriesType.running_stance_time_balance in types

    def test_granularity_one_second(self, running: FitParseResult) -> None:
        hr = sorted(s.recorded_at for s in running.samples if s.series_type == SeriesType.heart_rate)
        gaps = [(hr[i + 1] - hr[i]).seconds for i in range(len(hr) - 1)]
        assert all(g == 1 for g in gaps)

    def test_heart_rate_values_in_range(self, running: FitParseResult) -> None:
        vals = [s.value for s in running.samples if s.series_type == SeriesType.heart_rate]
        assert all(Decimal(40) <= v <= Decimal(220) for v in vals)

    def test_speed_non_negative(self, running: FitParseResult) -> None:
        vals = [s.value for s in running.samples if s.series_type == SeriesType.speed]
        assert all(v >= Decimal(0) for v in vals)

    def test_vertical_oscillation_in_cm_range(self, running: FitParseResult) -> None:
        # FIT raw mm/10 → parser _scale(0.1) → cm; synthetic value 8.5 cm
        vals = [s.value for s in running.samples if s.series_type == SeriesType.running_vertical_oscillation]
        assert all(Decimal("0.1") <= v <= Decimal("30") for v in vals)

    def test_sample_metadata(self, running: FitParseResult) -> None:
        s = running.samples[0]
        assert s.user_id == USER_ID
        assert s.data_source_id == DS_ID
        assert s.source == "garmin"
        assert s.recorded_at is not None
        assert s.recorded_at.tzinfo is not None

    def test_gps_values_scaled_degree_range(self, running: FitParseResult) -> None:
        lats = [s.value for s in running.samples if s.series_type == SeriesType.latitude_e4]
        lons = [s.value for s in running.samples if s.series_type == SeriesType.longitude_e4]
        assert lats
        assert lons
        assert all(Decimal(-900_000) <= v <= Decimal(900_000) for v in lats)
        assert all(Decimal(-1_800_000) <= v <= Decimal(1_800_000) for v in lons)
        # synthetic 50°N, stored x10,000 and within 1e-6 degree after scaling back
        assert all(abs(v / 10_000 - Decimal(50)) < Decimal("0.000001") for v in lats)

    def test_elevation_in_meters(self, running: FitParseResult) -> None:
        vals = [s.value for s in running.samples if s.series_type == SeriesType.elevation]
        # synthetic: 200 m
        assert all(Decimal(0) <= v <= Decimal(9000) for v in vals)

    def test_temperature_in_celsius_range(self, running: FitParseResult) -> None:
        vals = [s.value for s in running.samples if s.series_type == SeriesType.air_temperature]
        # synthetic: 18°C
        assert all(Decimal(-50) <= v <= Decimal(60) for v in vals)

    def test_running_dynamics_in_range(self, running: FitParseResult) -> None:
        vr = [s.value for s in running.samples if s.series_type == SeriesType.running_vertical_ratio]
        stb = [s.value for s in running.samples if s.series_type == SeriesType.running_stance_time_balance]
        # synthetic: vertical_ratio=8.5%, stance_time_balance=49.5%
        assert all(Decimal(0) <= v <= Decimal(100) for v in vr)
        assert all(Decimal(0) <= v <= Decimal(100) for v in stb)


class TestCycling:
    def test_only_heart_rate(self, cycling: FitParseResult) -> None:
        assert {s.series_type for s in cycling.samples} == {SeriesType.heart_rate}

    def test_has_samples(self, cycling: FitParseResult) -> None:
        assert len(cycling.samples) > 0


class TestSwimming:
    def test_only_heart_rate(self, swimming: FitParseResult) -> None:
        assert {s.series_type for s in swimming.samples} == {SeriesType.heart_rate}


class TestSegments:
    def test_running_has_two_laps(self, running: FitParseResult) -> None:
        laps = [s for s in running.segments if s["kind"] == "lap"]
        assert len(laps) == 2

    def test_lap_required_fields(self, running: FitParseResult) -> None:
        for lap in running.segments:
            assert lap["kind"] == "lap"
            assert isinstance(lap["index"], int)
            assert lap["elapsed_seconds"] > 0
            assert lap["start_time"] is not None

    def test_lap_has_distance_and_hr(self, running: FitParseResult) -> None:
        lap = running.segments[0]
        assert "distance_meters" in lap
        assert lap["distance_meters"] > 0
        assert "avg_heart_rate" in lap
        assert 40 <= lap["avg_heart_rate"] <= 220
        assert "max_heart_rate" in lap
        assert lap["max_heart_rate"] >= lap["avg_heart_rate"]

    def test_lap_indices_sequential(self, running: FitParseResult) -> None:
        laps = [s for s in running.segments if s["kind"] == "lap"]
        indices = [lap["index"] for lap in laps]
        assert indices == list(range(len(laps)))

    def test_cycling_no_segments(self, cycling: FitParseResult) -> None:
        assert cycling.segments == []

    def test_swimming_no_segments(self, swimming: FitParseResult) -> None:
        assert swimming.segments == []


class TestInvalidInput:
    def test_empty_bytes_returns_no_samples(self) -> None:
        assert parse_fit_file(b"", uuid4(), uuid4()).samples == []

    def test_garbage_bytes_raises(self) -> None:
        with pytest.raises(fitdecode.FitError):
            parse_fit_file(b"not a fit file at all", uuid4(), uuid4())


class TestStrengthSets:
    """Garmin writes one `set` message per set of a Strength activity; work sets
    become segments of kind "set" (rest periods dropped), which the samples
    endpoint returns as `sets` and HOS turns into a muscle map."""

    @pytest.fixture(scope="class")
    def strength(self) -> FitParseResult:
        return parse_fit_file(make_strength_fit(), USER_ID, DS_ID, source="garmin")

    def test_rest_periods_are_left_out(self, strength: FitParseResult) -> None:
        sets = [s for s in strength.segments if s["kind"] == "set"]
        assert len(sets) == 3

    def test_named_sets_carry_exercise_reps_and_weight(self, strength: FitParseResult) -> None:
        squat, bench, _ = [s for s in strength.segments if s["kind"] == "set"]
        assert squat["exercise_category"] == "squat"
        assert squat["repetitions"] == 8
        assert squat["weight_kg"] == 60.0
        assert squat["elapsed_seconds"] == 40.0
        assert bench["exercise_category"] == "bench_press"

    def test_an_unnamed_set_says_unknown(self, strength: FitParseResult) -> None:
        last = [s for s in strength.segments if s["kind"] == "set"][-1]
        assert last["exercise_category"] == "unknown"
        assert last["repetitions"] == 12

    def test_samples_endpoint_shape(self, strength: FitParseResult) -> None:
        sets = WorkoutSamplesService._sets(strength.segments)
        assert [s.exercise_category for s in sets] == ["squat", "bench_press", "unknown"]
        assert sets[0].duration_seconds == 40.0
        assert WorkoutSamplesService._laps(strength.segments) == []
