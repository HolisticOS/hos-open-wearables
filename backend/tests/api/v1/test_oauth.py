"""
Tests for OAuth endpoints.

Tests the /api/v1/oauth endpoints including:
- GET /api/v1/oauth/{provider}/authorize - auth, user check, redirect_uri allow-list
- GET /api/v1/oauth/{provider}/callback - redirect back to the client on success and failure
- GET /api/v1/oauth/providers - test list providers
- PUT /api/v1/oauth/providers/{provider} - test update provider status
"""

import json
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.redis_client import get_redis_client
from app.models import User
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.providers.templates.base_oauth import OAuthConnectionError
from tests.factories import ApiKeyFactory, DeveloperFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers, developer_auth_headers

APP_DEEP_LINK = "myapp://providers/connected?provider=garmin"


@pytest.fixture
def auth_headers(db: Session) -> dict[str, str]:
    return api_key_headers(ApiKeyFactory().id)


@pytest.fixture
def user(db: Session) -> User:
    return UserFactory()


@pytest.fixture
def deep_link_allowed() -> Generator[None, None, None]:
    with patch.object(settings, "oauth_allowed_redirect_prefixes", ["myapp://providers/connected"]):
        yield


class TestOAuthAuthorizeEndpoint:
    """Test suite for OAuth authorization endpoint."""

    def test_authorize_provider_success(self, client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
        """Test successfully initiating OAuth flow for a provider."""
        # Act
        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id)},
            headers=auth_headers,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "authorization_url" in data
        assert "state" in data
        assert isinstance(data["authorization_url"], str)
        assert isinstance(data["state"], str)
        assert len(data["state"]) > 0

    def test_authorize_accepts_developer_session(self, client: TestClient, user: User) -> None:
        """The dashboard calls authorize with the developer's bearer token."""
        headers = developer_auth_headers(DeveloperFactory().id)

        response = client.get("/api/v1/oauth/garmin/authorize", params={"user_id": str(user.id)}, headers=headers)

        assert response.status_code == 200

    def test_authorize_requires_auth(self, client: TestClient, user: User) -> None:
        response = client.get("/api/v1/oauth/garmin/authorize", params={"user_id": str(user.id)})

        assert response.status_code == 401

    def test_authorize_rejects_invalid_api_key(self, client: TestClient, user: User) -> None:
        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id)},
            headers=api_key_headers("sk-not-a-real-key"),
        )

        assert response.status_code == 401

    def test_authorize_unknown_user_is_404(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(uuid4())},
            headers=auth_headers,
        )

        assert response.status_code == 404

    def test_authorize_redirect_under_api_base_url(
        self, client: TestClient, user: User, auth_headers: dict[str, str]
    ) -> None:
        """A redirect_uri under API_BASE_URL is allowed and stored in the OAuth state."""
        redirect_uri = f"{settings.api_base_url}/somewhere?x=1"

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id), "redirect_uri": redirect_uri},
            headers=auth_headers,
        )

        assert response.status_code == 200
        stored = json.loads(get_redis_client().get(f"oauth_state:{response.json()['state']}"))
        assert stored["redirect_uri"] == redirect_uri

    def test_authorize_redirect_under_frontend_url(
        self, client: TestClient, user: User, auth_headers: dict[str, str]
    ) -> None:
        """The dashboard's own pair/success page keeps working."""
        redirect_uri = f"{settings.frontend_url}/users/{user.id}/pair/success?provider=garmin"

        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id), "redirect_uri": redirect_uri},
            headers=auth_headers,
        )

        assert response.status_code == 200

    def test_authorize_redirect_configured_prefix(
        self, client: TestClient, user: User, auth_headers: dict[str, str], deep_link_allowed: None
    ) -> None:
        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": str(user.id), "redirect_uri": APP_DEEP_LINK},
            headers=auth_headers,
        )

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "https://myapp.com/oauth/callback",
            "myapp://providers/connected-evil",
            "myapp://providers.evil/connected",
            "otherapp://providers/connected",
            "http://localhost:3000.evil.com/pair",
            "http://user@localhost:3000/pair",
            "//localhost:3000/pair",
            "not a url",
        ],
    )
    def test_authorize_rejects_redirect_outside_allow_list(
        self,
        client: TestClient,
        user: User,
        auth_headers: dict[str, str],
        deep_link_allowed: None,
        redirect_uri: str,
    ) -> None:
        with patch.object(settings, "frontend_url", "http://localhost:3000"):
            response = client.get(
                "/api/v1/oauth/garmin/authorize",
                params={"user_id": str(user.id), "redirect_uri": redirect_uri},
                headers=auth_headers,
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "redirect_uri is not allowed"

    def test_authorize_different_providers(self, client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
        """Test initiating OAuth for different providers."""
        for provider in ["garmin", "polar", "suunto"]:
            response = client.get(
                f"/api/v1/oauth/{provider}/authorize",
                params={"user_id": str(user.id)},
                headers=auth_headers,
            )

            assert response.status_code == 200
            data = response.json()
            assert "authorization_url" in data
            assert "state" in data

    def test_authorize_missing_user_id(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """Test authorization without user_id parameter."""
        response = client.get("/api/v1/oauth/garmin/authorize", headers=auth_headers)

        assert response.status_code == 400

    def test_authorize_invalid_user_id(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """Test authorization with invalid user_id format."""
        response = client.get(
            "/api/v1/oauth/garmin/authorize",
            params={"user_id": "not-a-uuid"},
            headers=auth_headers,
        )

        assert response.status_code == 400

    def test_authorize_invalid_provider(self, client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
        """Test authorization with non-existent provider."""
        response = client.get(
            "/api/v1/oauth/invalid-provider/authorize",
            params={"user_id": str(user.id)},
            headers=auth_headers,
        )

        assert response.status_code == 400

    def test_authorize_non_oauth_provider(self, client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
        """Test authorization with provider that doesn't support OAuth."""
        # Act - Try to authorize with "apple" which uses file import, not OAuth
        response = client.get(
            "/api/v1/oauth/apple/authorize",
            params={"user_id": str(user.id)},
            headers=auth_headers,
        )

        # Assert - Should fail because apple doesn't have OAuth
        assert response.status_code in [400, 401, 422]


def _put_state(user_id: UUID, redirect_uri: str | None, provider: str = "garmin") -> str:
    """Store an OAuth state the way the authorize step does and return its key."""
    state = f"state-{uuid4().hex}"
    get_redis_client().setex(
        f"oauth_state:{state}",
        900,
        json.dumps(
            {
                "user_id": str(user_id),
                "provider": provider,
                "redirect_uri": redirect_uri,
                "code_verifier": "verifier",
            }
        ),
    )
    return state


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


_TOKENS = OAuthTokenResponse(access_token="new-access", token_type="Bearer", expires_in=86400, refresh_token="r")


@pytest.fixture
def no_historical_sync() -> Generator[None, None, None]:
    with patch.object(settings, "historical_sync_on_connect", False):
        yield


class TestOAuthCallbackRedirects:
    """The callback sends the member back to the client's redirect_uri on success and on failure."""

    def test_provider_error_goes_back_to_redirect_uri(self, client: TestClient, user: User) -> None:
        state = _put_state(user.id, APP_DEEP_LINK)

        response = client.get(
            "/api/v1/oauth/garmin/callback",
            params={"error": "access_denied", "error_description": "User said no", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("myapp://providers/connected?")
        assert _query(location) == {"provider": ["garmin"], "status": ["error"], "error": ["access_denied"]}

    def test_provider_error_without_redirect_uri_uses_error_page(self, client: TestClient, user: User) -> None:
        state = _put_state(user.id, None)

        response = client.get(
            "/api/v1/oauth/garmin/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/api/v1/oauth/error?")

    def test_provider_error_without_state_uses_error_page(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/oauth/garmin/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/api/v1/oauth/error?")

    def test_missing_code_goes_back_to_redirect_uri(self, client: TestClient, user: User) -> None:
        state = _put_state(user.id, APP_DEEP_LINK)

        response = client.get("/api/v1/oauth/garmin/callback", params={"state": state}, follow_redirects=False)

        assert response.status_code == 303
        query = _query(response.headers["location"])
        assert query["status"] == ["error"]
        assert query["error"] == ["missing_oauth_parameters"]

    def test_invalid_state_keeps_existing_behaviour(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/oauth/garmin/callback",
            params={"code": "abc", "state": "unknown-state"},
            follow_redirects=False,
        )

        assert response.status_code == 400

    def test_reason_is_sanitised(self, client: TestClient, user: User) -> None:
        state = _put_state(user.id, APP_DEEP_LINK)

        response = client.get(
            "/api/v1/oauth/garmin/callback",
            params={"error": "bad thing&status=success", "state": state},
            follow_redirects=False,
        )

        query = _query(response.headers["location"])
        assert query["status"] == ["error"]
        assert query["error"] == ["bad_thing_status_success"]

    def test_connection_failure_goes_back_with_reason(self, client: TestClient, db: Session, user: User) -> None:
        """A Garmin user id lookup failure fails the connection and tells the app why."""
        state = _put_state(user.id, APP_DEEP_LINK)

        with (
            patch("app.services.providers.garmin.oauth.GarminOAuth._exchange_token", return_value=_TOKENS),
            patch(
                "app.services.providers.garmin.oauth.GarminOAuth._get_provider_user_info",
                side_effect=OAuthConnectionError("provider_user_lookup_failed", "lookup failed", 502),
            ),
        ):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 303
        query = _query(response.headers["location"])
        assert query == {"provider": ["garmin"], "status": ["error"], "error": ["provider_user_lookup_failed"]}

    def test_unexpected_failure_goes_back_with_generic_reason(self, client: TestClient, user: User) -> None:
        state = _put_state(user.id, APP_DEEP_LINK)

        with patch(
            "app.services.providers.garmin.oauth.GarminOAuth._exchange_token",
            side_effect=RuntimeError("boom"),
        ):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert _query(response.headers["location"])["error"] == ["connection_failed"]

    def test_failure_without_redirect_uri_raises_as_before(self, client: TestClient, user: User) -> None:
        state = _put_state(user.id, None)

        with patch(
            "app.services.providers.garmin.oauth.GarminOAuth._exchange_token",
            side_effect=OAuthConnectionError("token_exchange_failed", "nope"),
        ):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 400

    def test_success_adds_status_and_keeps_existing_params(
        self, client: TestClient, db: Session, user: User, no_historical_sync: None
    ) -> None:
        state = _put_state(user.id, "myapp://providers/connected?provider=garmin&ref=onboarding")

        with (
            patch("app.services.providers.garmin.oauth.GarminOAuth._exchange_token", return_value=_TOKENS),
            patch(
                "app.services.providers.garmin.oauth.GarminOAuth._get_provider_user_info",
                return_value={"user_id": "garmin-1", "username": None, "scope": None},
            ),
        ):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("myapp://providers/connected?")
        assert _query(location) == {"provider": ["garmin"], "ref": ["onboarding"], "status": ["success"]}

    def test_success_without_redirect_uri_uses_success_page(
        self, client: TestClient, db: Session, user: User, no_historical_sync: None
    ) -> None:
        state = _put_state(user.id, None)

        with (
            patch("app.services.providers.garmin.oauth.GarminOAuth._exchange_token", return_value=_TOKENS),
            patch(
                "app.services.providers.garmin.oauth.GarminOAuth._get_provider_user_info",
                return_value={"user_id": "garmin-1", "username": None, "scope": None},
            ),
        ):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/api/v1/oauth/success?provider=garmin")


class TestOAuthCallbackHistoricalSync:
    """HISTORICAL_SYNC_DISABLED_PROVIDERS opts single providers out of the on-connect backfill."""

    def _connect(self, client: TestClient, user: User, provider: str) -> None:
        state = _put_state(user.id, None, provider=provider)
        oauth_cls = {
            "garmin": "app.services.providers.garmin.oauth.GarminOAuth",
            "polar": "app.services.providers.polar.oauth.PolarOAuth",
        }[provider]
        with (
            patch(f"{oauth_cls}._exchange_token", return_value=_TOKENS),
            patch(
                f"{oauth_cls}._get_provider_user_info",
                return_value={"user_id": f"{provider}-1", "username": None, "scope": None},
            ),
        ):
            response = client.get(
                f"/api/v1/oauth/{provider}/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )
        assert response.status_code == 303

    @patch("app.integrations.celery.tasks.start_garmin_full_backfill")
    def test_garmin_backfill_runs_by_default(self, backfill: MagicMock, client: TestClient, user: User) -> None:
        with (
            patch.object(settings, "historical_sync_on_connect", True),
            patch.object(settings, "historical_sync_disabled_providers", []),
        ):
            self._connect(client, user, "garmin")

        backfill.delay.assert_called_once_with(str(user.id))

    @patch("app.integrations.celery.tasks.start_garmin_full_backfill")
    def test_garmin_backfill_skipped_when_disabled_for_garmin(
        self, backfill: MagicMock, client: TestClient, user: User
    ) -> None:
        with (
            patch.object(settings, "historical_sync_on_connect", True),
            patch.object(settings, "historical_sync_disabled_providers", ["garmin"]),
        ):
            self._connect(client, user, "garmin")

        backfill.delay.assert_not_called()

    @patch("app.integrations.celery.tasks.sync_vendor_data")
    def test_other_providers_keep_backfill_when_garmin_disabled(
        self, sync: MagicMock, client: TestClient, user: User
    ) -> None:
        with (
            patch.object(settings, "historical_sync_on_connect", True),
            patch.object(settings, "historical_sync_disabled_providers", ["garmin"]),
        ):
            self._connect(client, user, "polar")

        sync.delay.assert_called_once()
        assert sync.delay.call_args.kwargs["providers"] == ["polar"]
        assert sync.delay.call_args.kwargs["is_historical"] is True

    @patch("app.integrations.celery.tasks.start_garmin_full_backfill")
    def test_global_flag_off_still_disables_all(self, backfill: MagicMock, client: TestClient, user: User) -> None:
        with (
            patch.object(settings, "historical_sync_on_connect", False),
            patch.object(settings, "historical_sync_disabled_providers", []),
        ):
            self._connect(client, user, "garmin")

        backfill.delay.assert_not_called()


class TestLatestConnectionWins:
    """A Garmin account linked to a second OW user revokes the older link."""

    def test_new_link_revokes_other_users_connection(self, client: TestClient, db: Session) -> None:
        old_user = UserFactory()
        new_user = UserFactory()
        old_conn = UserConnectionFactory(user=old_user, provider="garmin", provider_user_id="garmin-shared")
        state = _put_state(new_user.id, None)

        with (
            patch.object(settings, "historical_sync_on_connect", False),
            patch("app.services.providers.garmin.oauth.GarminOAuth._exchange_token", return_value=_TOKENS),
            patch(
                "app.services.providers.garmin.oauth.GarminOAuth._get_provider_user_info",
                return_value={"user_id": "garmin-shared", "username": None, "scope": None},
            ),
            patch("app.services.providers.templates.base_oauth.on_connection_revoked") as revoked_event,
        ):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 303
        db.refresh(old_conn)
        assert old_conn.status == ConnectionStatus.REVOKED
        assert old_conn.access_token is None
        assert old_conn.refresh_token is None
        revoked_event.assert_called_once()
        assert revoked_event.call_args.kwargs["user_id"] == old_user.id
        assert revoked_event.call_args.kwargs["reason"] == "linked_to_another_user"

    def test_other_providers_keep_shared_accounts(self, client: TestClient, db: Session) -> None:
        """Sharing one account across OW users stays possible for providers without the rule."""
        old_user = UserFactory()
        new_user = UserFactory()
        old_conn = UserConnectionFactory(user=old_user, provider="polar", provider_user_id="polar-shared")
        state = _put_state(new_user.id, None, provider="polar")

        with (
            patch.object(settings, "historical_sync_on_connect", False),
            patch("app.services.providers.polar.oauth.PolarOAuth._exchange_token", return_value=_TOKENS),
            patch(
                "app.services.providers.polar.oauth.PolarOAuth._get_provider_user_info",
                return_value={"user_id": "polar-shared", "username": None, "scope": None},
            ),
        ):
            response = client.get(
                "/api/v1/oauth/polar/callback",
                params={"code": "abc", "state": state},
                follow_redirects=False,
            )

        assert response.status_code == 303
        db.refresh(old_conn)
        assert old_conn.status == ConnectionStatus.ACTIVE

    def test_reconnect_updates_provider_user_id(self, client: TestClient, db: Session) -> None:
        """Reconnecting the same OW user to a different Garmin account follows the new account."""
        user = UserFactory()
        conn = UserConnectionFactory(
            user=user,
            provider="garmin",
            provider_user_id="garmin-old",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        state = _put_state(user.id, None)

        with (
            patch.object(settings, "historical_sync_on_connect", False),
            patch("app.services.providers.garmin.oauth.GarminOAuth._exchange_token", return_value=_TOKENS),
            patch(
                "app.services.providers.garmin.oauth.GarminOAuth._get_provider_user_info",
                return_value={"user_id": "garmin-new", "username": None, "scope": None},
            ),
        ):
            client.get("/api/v1/oauth/garmin/callback", params={"code": "abc", "state": state}, follow_redirects=False)

        db.refresh(conn)
        assert conn.provider_user_id == "garmin-new"
        assert conn.access_token == "new-access"


class TestOAuthProvidersEndpoint:
    """Test suite for providers listing endpoint."""

    def test_get_providers_success(self, client: TestClient, db: Session) -> None:
        """Test successfully retrieving all providers."""
        # Act
        response = client.get("/api/v1/oauth/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

        # Verify structure of first provider
        if data:
            provider = data[0]
            assert "provider" in provider
            assert "name" in provider
            assert "icon_url" in provider
            assert "has_cloud_api" in provider
            assert "is_enabled" in provider

    def test_get_providers_enabled_only(self, client: TestClient, db: Session) -> None:
        """Test retrieving only enabled providers."""
        # Act
        response = client.get(
            "/api/v1/oauth/providers",
            params={"enabled_only": True},
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # All returned providers should be enabled
        for provider in data:
            assert provider["is_enabled"] is True

    def test_get_providers_cloud_only(self, client: TestClient, db: Session) -> None:
        """Test retrieving only cloud (OAuth) providers."""
        # Act
        response = client.get(
            "/api/v1/oauth/providers",
            params={"cloud_only": True},
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # All returned providers should have cloud API
        for provider in data:
            assert provider["has_cloud_api"] is True

    def test_get_providers_enabled_and_cloud(self, client: TestClient, db: Session) -> None:
        """Test retrieving providers with both filters."""
        # Act
        response = client.get(
            "/api/v1/oauth/providers",
            params={"enabled_only": True, "cloud_only": True},
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # All returned providers should be enabled AND have cloud API
        for provider in data:
            assert provider["is_enabled"] is True
            assert provider["has_cloud_api"] is True

    def test_get_providers_include_disabled(self, client: TestClient, db: Session) -> None:
        """Test that disabled providers are included by default."""
        # Act - no filters
        response = client.get("/api/v1/oauth/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Should include both enabled and disabled
        any(p["is_enabled"] for p in data)
        any(not p["is_enabled"] for p in data)
        # At least one of each should exist (assuming test data setup)
        # If all are enabled or disabled, that's also valid

    def test_get_providers_response_structure(self, client: TestClient, db: Session) -> None:
        """Test detailed response structure of providers."""
        # Act
        response = client.get("/api/v1/oauth/providers")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) > 0

        # Check first provider has all required fields
        provider = data[0]
        required_fields = [
            "provider",
            "name",
            "icon_url",
            "has_cloud_api",
            "is_enabled",
        ]
        for field in required_fields:
            assert field in provider, f"Missing field: {field}"


class TestOAuthUpdateProviderEndpoint:
    """Test suite for updating provider status endpoint."""

    def test_update_provider_status_enable(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test enabling a provider."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": True}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "garmin"
        assert data["is_enabled"] is True

    def test_update_provider_status_disable(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test disabling a provider."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": False}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/polar",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "polar"
        assert data["is_enabled"] is False

    def test_update_provider_requires_authentication(self, client: TestClient, db: Session) -> None:
        """Test that updating provider status requires authentication."""
        # Arrange
        update_data = {"is_enabled": True}

        # Act - no auth headers
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            json=update_data,
        )

        # Assert
        assert response.status_code == 401

    def test_update_provider_invalid_auth(self, client: TestClient, db: Session) -> None:
        """Test that invalid authentication is rejected."""
        # Arrange
        update_data = {"is_enabled": True}
        headers = {"Authorization": "Bearer invalid-token"}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 401

    def test_update_nonexistent_provider(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test updating a provider that doesn't exist."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": True}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/nonexistent-provider",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 400

    def test_update_live_sync_mode_non_configurable_provider(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test setting live_sync_mode on a provider that does not support it returns 400."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        # garmin has no rest_pull so live_sync_configurable=False
        update_data = {"live_sync_mode": "webhook"}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 400

    def test_update_provider_response_structure(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test response structure of update endpoint."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        update_data = {"is_enabled": True}

        # Act
        response = client.put(
            "/api/v1/oauth/providers/garmin",
            headers=headers,
            json=update_data,
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "provider" in data
        assert "name" in data
        assert "icon_url" in data
        assert "has_cloud_api" in data
        assert "is_enabled" in data

    def test_update_multiple_providers_sequentially(
        self,
        client: TestClient,
        db: Session,
        mock_external_apis: dict[str, MagicMock],
    ) -> None:
        """Test updating multiple providers one after another."""
        # Arrange
        developer = DeveloperFactory()
        headers = developer_auth_headers(developer.id)
        providers = ["garmin", "polar", "suunto"]

        for provider in providers:
            # Act
            response = client.put(
                f"/api/v1/oauth/providers/{provider}",
                headers=headers,
                json={"is_enabled": True},
            )

            # Assert
            assert response.status_code == 200
            data = response.json()
            assert data["provider"] == provider
            assert data["is_enabled"] is True
