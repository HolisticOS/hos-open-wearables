"""When the top-priority source for a day looks faulty, which values to take from another one.

Provider and device priority still decide who leads. This only steps in when the leader was
barely worn (flat battery, left on the charger) and another source clearly saw more. A device
that was worn keeps its number even when the phone reads higher, because phones and watches
legitimately disagree and the watch is usually the better count.
"""

from dataclasses import dataclass
from datetime import date

from app.schemas.enums.device_type import DeviceType, infer_device_type_from_model

# A wearable with heart rate in fewer hours of the day than this was not really worn.
WORN_HOURS_MIN = 12

# Sleep has no wear signal of its own: a night far shorter than another source's is the fault.
SLEEP_SHORT_RATIO = 0.6
SLEEP_DONOR_MIN_MINUTES = 240

# These collect data from other devices rather than being a device themselves.
AGGREGATOR_PROVIDERS = frozenset({"apple", "samsung", "google", "unknown", "internal"})
WEARABLE_DEVICE_TYPES = frozenset({DeviceType.WATCH, DeviceType.BAND, DeviceType.RING})

# How a provider's own data appears inside an aggregator. It is the same device, so it can
# never prove that device wrong.
MIRROR_SOURCE_NAMES: dict[str, tuple[str, ...]] = {
    "garmin": ("connect", "garmin"),
    "whoop": ("whoop",),
    "oura": ("oura",),
    "polar": ("polar",),
    "suunto": ("suunto",),
    "fitbit": ("fitbit",),
    "ultrahuman": ("ultrahuman",),
}


@dataclass(frozen=True)
class MetricRule:
    """Move `keys` from a donor when its `measure` beats the leader's by both margins."""

    keys: tuple[str, ...]
    measure: str
    min_ratio: float
    min_gain: float


ACTIVITY_RULES: tuple[MetricRule, ...] = (
    MetricRule(("steps_sum",), "steps_sum", 1.5, 1000),
    MetricRule(("active_energy_sum",), "active_energy_sum", 1.5, 100),
    MetricRule(("basal_energy_sum",), "basal_energy_sum", 1.5, 200),
    # Heart rate has no "more", so the source that covered more of the day wins it.
    MetricRule(("hr_avg", "hr_max", "hr_min"), "hr_hours", 1.5, 4),
    MetricRule(("distance_sum",), "distance_sum", 1.5, 800),
    MetricRule(("flights_climbed_sum",), "flights_climbed_sum", 1.5, 3),
    MetricRule(("active_time_minutes",), "active_time_minutes", 1.5, 15),
)


def _provider(entry: dict) -> str:
    return str(entry.get("provider") or entry.get("source") or "unknown").lower()


def is_wearable(entry: dict) -> bool:
    """A device worn on the body, as opposed to a phone or an aggregator's unknown source."""
    if _provider(entry) not in AGGREGATOR_PROVIDERS:
        return True
    return infer_device_type_from_model(entry.get("device_model")) in WEARABLE_DEVICE_TYPES


def is_mirror(leader: dict, candidate: dict) -> bool:
    """The leader's own data, copied into an aggregator such as Apple Health."""
    if _provider(candidate) not in AGGREGATOR_PROVIDERS:
        return False
    source = str(candidate.get("source") or "").lower()
    return any(name in source for name in MIRROR_SOURCE_NAMES.get(_provider(leader), ()))


def rule_applies(day: date, start: date | None) -> bool:
    """Days before `start` keep the plain priority result, so history never changes."""
    return start is not None and day >= start


def apply_activity_fault_rule(entries_sorted: list[dict], winner: dict) -> dict:
    """The winner, with each metric the barely-worn leader under-counted taken from a donor.

    Args:
        entries_sorted: The day's sources, leader first.
        winner: The leader after the normal empty-metric fallback.
    """
    leader = entries_sorted[0]
    worn_hours = leader.get("hr_hours")
    if not is_wearable(leader) or worn_hours is None or worn_hours >= WORN_HOURS_MIN:
        return winner

    donors = [entry for entry in entries_sorted[1:] if not is_mirror(leader, entry)]
    if not donors:
        return winner

    result = winner
    for rule in ACTIVITY_RULES:
        donor = max(donors, key=lambda entry: entry.get(rule.measure) or 0)
        base = leader.get(rule.measure) or 0
        value = donor.get(rule.measure) or 0
        if value >= base * rule.min_ratio and value - base >= rule.min_gain:
            result = result if result is not winner else dict(winner)
            for key in rule.keys:
                result[key] = donor.get(key)
    return result


def apply_sleep_fault_rule(entries_sorted: list[dict], winner: dict) -> dict:
    """The donor's whole night when the wearable's is far shorter, otherwise the winner."""
    leader = entries_sorted[0]
    if not is_wearable(leader):
        return winner

    donors = [entry for entry in entries_sorted[1:] if not is_mirror(leader, entry)]
    if not donors:
        return winner

    donor = max(donors, key=lambda entry: entry.get("total_duration_minutes") or 0)
    minutes = donor.get("total_duration_minutes") or 0
    short = (leader.get("total_duration_minutes") or 0) < minutes * SLEEP_SHORT_RATIO
    return donor if minutes >= SLEEP_DONOR_MIN_MINUTES and short else winner
