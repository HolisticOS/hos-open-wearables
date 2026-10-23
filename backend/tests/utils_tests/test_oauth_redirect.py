"""Tests for the OAuth redirect allow-list, query building, and the list settings that feed them."""

from unittest.mock import patch

import pytest

from app.config import Settings, settings
from app.utils.oauth_redirect import add_query_params, is_allowed_redirect_uri, short_reason


@pytest.fixture(autouse=True)
def _bases() -> object:
    with (
        patch.object(settings, "api_base_url", "https://api.example.com"),
        patch.object(settings, "frontend_url", "https://dash.example.com"),
        patch.object(settings, "oauth_allowed_redirect_prefixes", ["myapp://providers/connected", "https://w.io/cb/"]),
    ):
        yield


class TestIsAllowedRedirectUri:
    @pytest.mark.parametrize(
        "uri",
        [
            "https://api.example.com/api/v1/oauth/success",
            "https://dash.example.com/users/1/pair/success?provider=garmin",
            "HTTPS://DASH.example.com/x",
            "myapp://providers/connected",
            "myapp://providers/connected?provider=garmin",
            "myapp://providers/connected/sub",
            "https://w.io/cb",
            "https://w.io/cb/next",
        ],
    )
    def test_allowed(self, uri: str) -> None:
        assert is_allowed_redirect_uri(uri) is True

    @pytest.mark.parametrize(
        "uri",
        [
            "https://evil.example.com/",
            "https://api.example.com.evil.com/",
            "https://attacker@api.example.com/",
            "http://api.example.com/",  # scheme must match
            "myapp://providers/connectedX",
            "myapp://providers/other",
            "myapp://evil/connected",
            "https://w.io/cbx",
            "/relative/path",
            "//dash.example.com/x",
            "https://dash.example.com/x y",
            "https://dash.example.com\\@evil.com",
            "",
        ],
    )
    def test_rejected(self, uri: str) -> None:
        assert is_allowed_redirect_uri(uri) is False

    def test_empty_setting_still_allows_the_two_bases(self) -> None:
        with patch.object(settings, "oauth_allowed_redirect_prefixes", []):
            assert is_allowed_redirect_uri("https://dash.example.com/pair") is True
            assert is_allowed_redirect_uri("myapp://providers/connected") is False


class TestAddQueryParams:
    def test_appends_missing_and_keeps_existing(self) -> None:
        url = add_query_params("myapp://providers/connected?provider=garmin", {"status": "success"}, override=False)
        assert url == "myapp://providers/connected?provider=garmin&status=success"

    def test_does_not_override_without_flag(self) -> None:
        url = add_query_params("https://x.io/p?provider=garmin", {"provider": "polar"}, override=False)
        assert url == "https://x.io/p?provider=garmin"

    def test_override_replaces(self) -> None:
        url = add_query_params("https://x.io/p?status=success&a=1", {"status": "error"}, override=True)
        assert url == "https://x.io/p?a=1&status=error"

    def test_encodes_values(self) -> None:
        url = add_query_params("https://x.io/p", {"error": "a b&c"}, override=True)
        assert url == "https://x.io/p?error=a+b%26c"


class TestShortReason:
    def test_cleans_and_truncates(self) -> None:
        assert short_reason("access_denied") == "access_denied"
        assert short_reason("bad value&x=1") == "bad_value_x_1"
        assert len(short_reason("x" * 500)) == 64
        assert short_reason(None) == "connection_failed"
        assert short_reason("&&&", default="provider_error") == "provider_error"


class TestListSettings:
    def test_comma_separated_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OAUTH_ALLOWED_REDIRECT_PREFIXES", "myapp://providers/connected, https://a.io/cb")
        monkeypatch.setenv("HISTORICAL_SYNC_DISABLED_PROVIDERS", "Garmin, polar")
        monkeypatch.setenv("DELETE_PROVIDER_DATA_ON_DISCONNECT", "false")

        loaded = Settings(secret_key="x")  # ty: ignore[missing-argument]

        assert loaded.oauth_allowed_redirect_prefixes == ["myapp://providers/connected", "https://a.io/cb"]
        assert loaded.historical_sync_disabled_providers == ["garmin", "polar"]
        assert loaded.delete_provider_data_on_disconnect is False

    def test_json_list_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HISTORICAL_SYNC_DISABLED_PROVIDERS", '["garmin"]')

        assert Settings(secret_key="x").historical_sync_disabled_providers == ["garmin"]  # ty: ignore[missing-argument]

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "OAUTH_ALLOWED_REDIRECT_PREFIXES",
            "HISTORICAL_SYNC_DISABLED_PROVIDERS",
            "DELETE_PROVIDER_DATA_ON_DISCONNECT",
        ):
            monkeypatch.delenv(name, raising=False)

        loaded = Settings(secret_key="x", _env_file=None)  # ty: ignore[missing-argument, unknown-argument]

        assert loaded.oauth_allowed_redirect_prefixes == []
        assert loaded.historical_sync_disabled_providers == []
        assert loaded.delete_provider_data_on_disconnect is True
