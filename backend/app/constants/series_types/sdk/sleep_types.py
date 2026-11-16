from enum import StrEnum

from app.constants.sleep import SleepStageType


class SleepPhase(StrEnum):
    IN_BED = SleepStageType.IN_BED
    SLEEPING = "sleeping"
    AWAKE = SleepStageType.AWAKE
    ASLEEP_LIGHT = SleepStageType.LIGHT
    ASLEEP_DEEP = SleepStageType.DEEP
    ASLEEP_REM = SleepStageType.REM
    UNKNOWN = SleepStageType.UNKNOWN


# Health Connect SleepSessionRecord.STAGE_TYPE_* by its int code. The Android SDK maps these to the
# lowercase SleepPhase values itself (AWAKE_IN_BED -> "in_bed", OUT_OF_BED -> "unknown"); the names and
# codes are accepted too, for SDK builds or other writers that pass Health Connect's own form through.
# Time out of bed and awake in bed is time awake inside the session, so both count as awake.
HEALTH_CONNECT_STAGE_CODES: dict[int, SleepPhase] = {
    0: SleepPhase.UNKNOWN,
    1: SleepPhase.AWAKE,
    2: SleepPhase.SLEEPING,
    3: SleepPhase.AWAKE,  # OUT_OF_BED
    4: SleepPhase.ASLEEP_LIGHT,
    5: SleepPhase.ASLEEP_DEEP,
    6: SleepPhase.ASLEEP_REM,
    7: SleepPhase.AWAKE,  # AWAKE_IN_BED
}

_HEALTH_CONNECT_STAGE_PREFIX = "stage_type_"

# Normalized (lowercase, "_"-separated, prefix stripped) spellings that are not a SleepPhase value.
_STAGE_ALIASES: dict[str, SleepPhase] = {
    # Health Connect STAGE_TYPE_* names
    "out_of_bed": SleepPhase.AWAKE,
    "awake_in_bed": SleepPhase.AWAKE,
    # HealthKit HKCategoryValueSleepAnalysis names
    "inbed": SleepPhase.IN_BED,
    "asleep": SleepPhase.SLEEPING,
    "asleep_unspecified": SleepPhase.SLEEPING,
    "asleepunspecified": SleepPhase.SLEEPING,
    "asleep_core": SleepPhase.ASLEEP_LIGHT,
    "asleepcore": SleepPhase.ASLEEP_LIGHT,
    "core": SleepPhase.ASLEEP_LIGHT,
    "asleep_deep": SleepPhase.ASLEEP_DEEP,
    "asleepdeep": SleepPhase.ASLEEP_DEEP,
    "asleep_rem": SleepPhase.ASLEEP_REM,
    "asleeprem": SleepPhase.ASLEEP_REM,
}


def get_apple_sleep_phase(apple_sleep_phase: str | int | None) -> SleepPhase | None:
    """The SleepPhase of an SDK sleep entry's stage, or None when the stage is not recognised.

    Accepts the SDK's own lowercase values (Apple, Samsung and Health Connect all send them), Health
    Connect's STAGE_TYPE_* names in any case with or without the prefix, its int codes, and the
    HealthKit sleep-analysis names.
    """
    if apple_sleep_phase is None:
        return None
    if isinstance(apple_sleep_phase, int) and not isinstance(apple_sleep_phase, bool):
        return HEALTH_CONNECT_STAGE_CODES.get(apple_sleep_phase)
    raw = str(apple_sleep_phase).strip()
    try:
        return SleepPhase(raw)
    except ValueError:
        pass
    key = raw.lower().replace("-", "_").replace(" ", "_")
    key = key.removeprefix(_HEALTH_CONNECT_STAGE_PREFIX)
    if key.isdigit():
        return HEALTH_CONNECT_STAGE_CODES.get(int(key))
    try:
        return SleepPhase(key)
    except ValueError:
        return _STAGE_ALIASES.get(key)
