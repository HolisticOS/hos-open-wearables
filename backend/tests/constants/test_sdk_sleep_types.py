"""SDK sleep stage names: every spelling the SDKs send maps to a SleepPhase, anything else to None."""

import pytest

from app.constants.series_types.sdk import SleepPhase, get_apple_sleep_phase


@pytest.mark.parametrize(
    ("stage", "phase"),
    [
        # What the Android SDK sends for Health Connect (HealthConnectManager.mapSleepStage), Samsung and Apple
        ("awake", SleepPhase.AWAKE),
        ("sleeping", SleepPhase.SLEEPING),
        ("in_bed", SleepPhase.IN_BED),
        ("light", SleepPhase.ASLEEP_LIGHT),
        ("deep", SleepPhase.ASLEEP_DEEP),
        ("rem", SleepPhase.ASLEEP_REM),
        ("unknown", SleepPhase.UNKNOWN),
        # Health Connect's SleepSessionRecord.STAGE_TYPE_* names, in any case, with or without the prefix
        ("STAGE_TYPE_UNKNOWN", SleepPhase.UNKNOWN),
        ("STAGE_TYPE_AWAKE", SleepPhase.AWAKE),
        ("STAGE_TYPE_SLEEPING", SleepPhase.SLEEPING),
        ("STAGE_TYPE_OUT_OF_BED", SleepPhase.AWAKE),
        ("STAGE_TYPE_LIGHT", SleepPhase.ASLEEP_LIGHT),
        ("STAGE_TYPE_DEEP", SleepPhase.ASLEEP_DEEP),
        ("STAGE_TYPE_REM", SleepPhase.ASLEEP_REM),
        ("STAGE_TYPE_AWAKE_IN_BED", SleepPhase.AWAKE),
        ("stage_type_deep", SleepPhase.ASLEEP_DEEP),
        ("OUT_OF_BED", SleepPhase.AWAKE),
        ("awake_in_bed", SleepPhase.AWAKE),
        ("Awake-In-Bed", SleepPhase.AWAKE),
        ("LIGHT", SleepPhase.ASLEEP_LIGHT),
        ("REM", SleepPhase.ASLEEP_REM),
        (" deep ", SleepPhase.ASLEEP_DEEP),
        # Health Connect's int codes, raw or as strings
        (0, SleepPhase.UNKNOWN),
        (1, SleepPhase.AWAKE),
        (2, SleepPhase.SLEEPING),
        (3, SleepPhase.AWAKE),
        (4, SleepPhase.ASLEEP_LIGHT),
        (5, SleepPhase.ASLEEP_DEEP),
        (6, SleepPhase.ASLEEP_REM),
        (7, SleepPhase.AWAKE),
        ("5", SleepPhase.ASLEEP_DEEP),
        # HealthKit sleep-analysis names
        ("inBed", SleepPhase.IN_BED),
        ("asleepCore", SleepPhase.ASLEEP_LIGHT),
        ("asleepDeep", SleepPhase.ASLEEP_DEEP),
        ("asleepREM", SleepPhase.ASLEEP_REM),
        ("asleepUnspecified", SleepPhase.SLEEPING),
    ],
)
def test_known_stage_spellings(stage: str | int, phase: SleepPhase) -> None:
    assert get_apple_sleep_phase(stage) == phase


@pytest.mark.parametrize("stage", ["napping", "", "STAGE_TYPE_NAP", 9, "42", None])
def test_unknown_stages_are_none(stage: str | int | None) -> None:
    assert get_apple_sleep_phase(stage) is None
