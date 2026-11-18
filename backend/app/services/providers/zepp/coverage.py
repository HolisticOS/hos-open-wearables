"""What Zepp (Amazfit) emits.

Zepp's push API mirrors Garmin's Health API, so sleep, HRV and the "upcoming"
dailies / pulse ox / body composition / skin temperature pushes go through
Garmin's builders (``Garmin247Data``); workouts come from the activity FIT file.
"""

from app.schemas.enums import SeriesType, public_series_type
from app.schemas.enums.health_score_category import HealthScoreCategory
from app.services.providers.garmin.coverage import ACTIVITY_SAMPLE_SERIES, DAILIES_SERIES

TIMESERIES: frozenset[SeriesType] = frozenset(
    {
        *(public_series_type(st) for _, st in ACTIVITY_SAMPLE_SERIES),  # FIT records
        SeriesType.running_vertical_oscillation,  # FIT records
        SeriesType.running_ground_contact_time,  # FIT records
        SeriesType.running_stride_length,  # FIT records
        SeriesType.running_vertical_ratio,  # FIT records
        SeriesType.running_stance_time_balance,  # FIT records
        *(st for _, st in DAILIES_SERIES),  # dailies (upcoming)
        SeriesType.heart_rate_variability_rmssd,  # hrv
        SeriesType.oxygen_saturation,  # pulseOx (upcoming)
        SeriesType.weight,  # bodyComps (upcoming)
        SeriesType.body_fat_percentage,
        SeriesType.body_mass_index,
        SeriesType.skeletal_muscle_mass,
        SeriesType.bone_mass,
        SeriesType.body_water_percentage,
        SeriesType.skin_temperature_deviation,  # skinTemp (upcoming)
    }
)

WORKOUT_FIELDS: frozenset[str] = frozenset(
    {
        "heart_rate_max",
        "heart_rate_avg",
        "energy_burned",
        "distance",
        "average_cadence",
        "average_speed",
        "max_speed",
        "moving_time_seconds",
        "total_elevation_gain",
        "hr_zones",
        "power_zones",
        "segments",
    }
)

SLEEP_FIELDS: frozenset[str] = frozenset(
    {
        "sleep_total_duration_minutes",
        "sleep_time_in_bed_minutes",
        "sleep_efficiency_score",
        "sleep_deep_minutes",
        "sleep_rem_minutes",
        "sleep_light_minutes",
        "sleep_awake_minutes",
        "is_nap",
        "sleep_stages",
    }
)

MENSTRUAL_CYCLE_FIELDS: frozenset[str] = frozenset()

HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset({HealthScoreCategory.SLEEP})

# A FIT session's total_calories counts resting energy too.
WORKOUT_ENERGY_SERIES = SeriesType.total_energy
