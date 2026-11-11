from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import EntrySource, WorkoutIntensity
from app.schemas.model_crud.activities import SleepStage
from app.schemas.model_crud.activities.zones import HRZones, PowerZones
from app.schemas.utils import SourceMetadata

from .summaries import SleepStagesSummary


class WorkoutTotals(BaseModel):
    """Workouts matching a filter, added up in the database rather than paged."""

    count: int = Field(..., description="Workouts that match the filters", example=42)
    duration_seconds: int = Field(..., description="Sum of their durations", example=95400)
    calories_kcal: float | None = Field(
        None, description="Sum of reported energy burned; null when none reported any", example=18250.5
    )
    distance_meters: float | None = Field(
        None, description="Sum of reported distance; null when none reported any", example=212400.0
    )


class Workout(BaseModel):
    id: UUID
    type: str  # Should be WorkoutType enum ideally
    name: str | None = Field(None, example="Morning Run")
    start_time: datetime
    end_time: datetime
    zone_offset: str | None = None
    duration_seconds: int | None = None
    source: SourceMetadata
    entry_source: EntrySource | None = Field(
        None,
        description="How the workout was recorded, normalized across providers.",
        examples=["automatic"],
    )
    intensity: WorkoutIntensity | None = Field(
        None,
        description="Subjective intensity of the workout, normalized across providers.",
        examples=["moderate"],
    )
    calories_kcal: float | None = None
    distance_meters: float | None = None
    avg_heart_rate_bpm: int | None = None
    max_heart_rate_bpm: int | None = None
    avg_pace_sec_per_km: int | float | None = None
    elevation_gain_meters: float | None = None
    heart_rate_min: int | None = None
    steps_count: int | None = None
    average_speed: float | None = None
    max_speed: float | None = None
    average_cadence: float | None = None
    average_watts: float | None = None
    max_watts: float | None = None
    average_met: float | None = Field(
        None, description="The workout's average metabolic equivalent (MET), from its physical_effort series."
    )
    moving_time_seconds: int | None = None
    elev_high: float | None = None
    elev_low: float | None = None
    hr_zones: HRZones | None = None
    power_zones: PowerZones | None = None
    segments: list[dict] | None = None


class Macros(BaseModel):
    protein_g: float | None = None
    carbohydrates_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None


class NutrientValue(BaseModel):
    value: float
    unit: str


class Meal(BaseModel):
    id: UUID
    timestamp: datetime
    meal_type: str | None = None
    name: str | None = None
    source: SourceMetadata
    calories_kcal: float | None = None
    macros: Macros | None = None
    water_ml: float | None = None
    nutrients: dict[str, NutrientValue] = Field(
        default_factory=dict,
        description="All nutrient values recorded for the meal, keyed by series type (e.g. dietary_sugar)",
        example={"dietary_sugar": {"value": 12.5, "unit": "g"}},
    )


class Measurement(BaseModel):
    id: UUID
    type: Literal["weight", "blood_pressure", "body_composition", "temperature", "blood_glucose"]
    timestamp: datetime
    source: SourceMetadata
    values: dict[str, float | str] = Field(..., description="Measurement-specific values", example={"weight_kg": 72.5})


class SleepTotals(BaseModel):
    """Sleep sessions matching a filter, added up in the database rather than paged."""

    count: int = Field(..., description="Sessions that match the filters, naps included", example=30)
    naps: int = Field(..., description="Of those, how many are naps", example=4)
    sleep_duration_seconds: int = Field(..., description="Sum of time asleep", example=799200)
    time_in_bed_seconds: int = Field(
        ..., description="Sum of time in bed, or of the session's span where none was reported", example=871200
    )
    avg_efficiency_percent: float | None = Field(
        None, description="Mean efficiency over the sessions that report one", example=91.4
    )


class SleepSession(BaseModel):
    id: UUID
    start_time: datetime
    end_time: datetime
    zone_offset: str | None = None
    source: SourceMetadata
    duration_seconds: int = Field(
        ...,
        description=(
            "Time from start_time to end_time, awake time included; for a night merged from several "
            "sessions, the gaps between them too"
        ),
        example=28800,
    )
    sleep_duration_seconds: int | None = Field(
        None, description="Time asleep, awake time excluded; whole minutes, so a multiple of 60", example=25800
    )
    time_in_bed_seconds: int | None = Field(
        None, description="Time in bed, awake time included; whole minutes, so a multiple of 60", example=28200
    )
    efficiency_percent: float | None = None
    stages: SleepStagesSummary | None = None
    sleep_stage_intervals: list[SleepStage] | None = None
    is_nap: bool = Field(
        False,
        description=(
            "Whether the session is a nap: from the provider when it flags naps, else derived by Open Wearables "
            "(see Data types)"
        ),
    )
    is_final: bool = Field(
        ...,
        description="Whether no more data is expected for this session; until then the provider may still extend it",
        example=True,
    )


class MenstrualCycleRecord(BaseModel):
    id: UUID
    start_time: datetime
    end_time: datetime
    zone_offset: str | None = None
    source: SourceMetadata
    current_phase: int | None = None
    current_phase_type: str | None = None
    day_in_cycle: int | None = None
    cycle_length: int | None = None
    predicted_cycle_length: int | None = None
    is_predicted_cycle: bool | None = None
    period_length: int | None = None
    length_of_current_phase: int | None = None
    days_until_next_phase: int | None = None
    fertile_window_start: int | None = None
    length_of_fertile_window: int | None = None
    last_updated_at: datetime | None = None
    has_specified_cycle_length: bool | None = None
    has_specified_period_length: bool | None = None
    pregnancy_snapshot: list[dict] | None = None


class WorkoutSampleSeries(BaseModel):
    unit: str
    points: list[tuple[datetime, float]] = Field(
        default_factory=list, description="[timestamp, value] pairs, one per time bucket (bucket average)"
    )


class WorkoutLap(BaseModel):
    """One lap from the workout's FIT file. Extra lap metrics from the file are passed through as-is."""

    model_config = ConfigDict(extra="allow")

    index: int
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: float | None = None
    distance_meters: float | None = None
    avg_heart_rate_bpm: float | None = None
    max_heart_rate_bpm: float | None = None
    avg_speed_mps: float | None = None
    max_speed_mps: float | None = None


class WorkoutSet(BaseModel):
    """One work set from a strength activity's FIT file (rest periods are left out).

    `exercise_category` is the FIT exercise_category name (squat, bench_press, row,
    ...) or "unknown" when the watch could not tell; `exercise_subtype` is the
    category-specific sub-type as the file stores it.
    """

    index: int
    start_time: datetime | None = None
    duration_seconds: float | None = None
    exercise_category: str = "unknown"
    exercise_subtype: str | None = None
    repetitions: int | None = None
    weight_kg: float | None = None


class HeartRateZoneRead(BaseModel):
    zone: int
    min_bpm: int | None = None
    max_bpm: int | None = None
    seconds: float


class PowerZoneRead(BaseModel):
    zone: int
    min_watts: int | None = None
    max_watts: int | None = None
    seconds: float


class WorkoutSamples(BaseModel):
    """Downsampled per-workout samples, route, laps and zones."""

    workout_id: UUID
    start_time: datetime
    end_time: datetime
    source: SourceMetadata
    series: dict[str, WorkoutSampleSeries] = Field(default_factory=dict)
    route: list[tuple[datetime, float, float]] = Field(
        default_factory=list, description="[timestamp, latitude, longitude]; real recorded points, never averaged"
    )
    laps: list[WorkoutLap] = Field(default_factory=list)
    sets: list[WorkoutSet] = Field(
        default_factory=list, description="Strength work sets from the FIT file; empty for other activities"
    )
    zones: dict[str, list[HeartRateZoneRead] | list[PowerZoneRead]] = Field(
        default_factory=dict, description="Time in zone from the FIT file; keys present only when stored"
    )
