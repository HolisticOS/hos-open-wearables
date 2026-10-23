from enum import Enum


class ProviderName(str, Enum):
    """Supported data providers."""

    APPLE = "apple"
    SAMSUNG = "samsung"
    GOOGLE = "google"
    GARMIN = "garmin"
    POLAR = "polar"
    SUUNTO = "suunto"
    WHOOP = "whoop"
    STRAVA = "strava"
    OURA = "oura"
    FITBIT = "fitbit"
    ULTRAHUMAN = "ultrahuman"
    UNKNOWN = "unknown"
    INTERNAL = "internal"

    @classmethod
    def from_source_string(cls, source: str | None) -> "ProviderName":
        """Infer provider from a source string by checking if provider name appears in it.

        Args:
            source: Source string (e.g., "apple_health_sdk", "Garmin Connect")

        Returns:
            Matching ProviderName or UNKNOWN if no match found
        """
        if not source:
            return cls.UNKNOWN

        source_lower = source.lower()
        # Check each provider (except UNKNOWN) to see if it appears in the source string
        for provider in cls:
            if provider in (cls.UNKNOWN, cls.INTERNAL):
                continue
            if provider.value in source_lower:
                return provider

        return cls.UNKNOWN


# Lower = preferred. A wearable the user connected (its own cloud account) beats the
# phone's health store: Apple Health, Samsung Health and Health Connect also hold copies
# of whatever other apps wrote into them, so they are the fallback for days and metrics
# the wearable has nothing for. Rows in provider_priority override these.
DEFAULT_PROVIDER_PRIORITY: dict[ProviderName, int] = {
    ProviderName.GARMIN: 1,
    ProviderName.POLAR: 2,
    ProviderName.SUUNTO: 3,
    ProviderName.WHOOP: 4,
    ProviderName.OURA: 5,
    ProviderName.FITBIT: 6,
    ProviderName.ULTRAHUMAN: 7,
    ProviderName.STRAVA: 8,
    ProviderName.APPLE: 20,
    ProviderName.SAMSUNG: 21,
    ProviderName.GOOGLE: 22,
}
