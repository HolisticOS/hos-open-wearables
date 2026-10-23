from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.model_crud.activities import SleepStage
from app.schemas.utils import SourceMetadata

from .data_point_responses import TimeSeriesSample
from .summaries import SleepStagesSummary


class Workout(BaseModel):
    id: UUID
    type: str  # Should be WorkoutType enum ideally
    name: str | None = Field(None, example="Morning Run")
    start_time: datetime
    end_time: datetime
    zone_offset: str | None = None
    duration_seconds: int | None = None
    source: SourceMetadata
    calories_kcal: float | None = None
    distance_meters: float | None = None
    avg_heart_rate_bpm: int | None = None
    max_heart_rate_bpm: int | None = None
    avg_pace_sec_per_km: int | float | None = None
    elevation_gain_meters: float | None = None


class WorkoutDetailed(Workout):
    heart_rate_samples: list[TimeSeriesSample] | None = None


class Macros(BaseModel):
    protein_g: float | None = None
    carbohydrates_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None


class Meal(BaseModel):
    id: UUID
    timestamp: datetime
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    name: str | None = None
    source: SourceMetadata
    calories_kcal: float | None = None
    macros: Macros | None = None
    water_ml: float | None = None


class Measurement(BaseModel):
    id: UUID
    type: Literal["weight", "blood_pressure", "body_composition", "temperature", "blood_glucose"]
    timestamp: datetime
    source: SourceMetadata
    values: dict[str, float | str] = Field(..., description="Measurement-specific values", example={"weight_kg": 72.5})


class SleepSession(BaseModel):
    id: UUID
    start_time: datetime
    end_time: datetime
    zone_offset: str | None = None
    source: SourceMetadata
    duration_seconds: int
    sleep_duration_seconds: int | None = None
    efficiency_percent: float | None = None
    stages: SleepStagesSummary | None = None
    sleep_stage_intervals: list[SleepStage] | None = None
    is_nap: bool = False


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
    zones: dict[str, list[HeartRateZoneRead] | list[PowerZoneRead]] = Field(
        default_factory=dict, description="Time in zone from the FIT file; keys present only when stored"
    )
