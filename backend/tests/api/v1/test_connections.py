"""
Tests for connections endpoints.

Tests the /api/v1/users/{user_id}/connections endpoint including:
- Get user connections
- Disconnect provider
- Authentication and authorization
- Connection status filtering
- Error cases
"""

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers

GARMIN_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"


@pytest.fixture(autouse=True)
def provider_network() -> Generator[dict[str, MagicMock], None, None]:
    """Disconnect may refresh a token and call the provider: never reach the real provider in tests."""
    with (
        patch("httpx.post", side_effect=httpx.ConnectError("no network in tests")) as post,
        patch("httpx.delete") as delete,
        patch("app.services.user_connection_service.schedule_provider_data_deletion") as schedule,
    ):
        yield {"post": post, "delete": delete, "schedule": schedule}


class TestConnectionsEndpoints:
    """Test suite for connections endpoints."""

    def test_get_connections_success(self, client: TestClient, db: Session) -> None:
        """Test successfully retrieving all connections for a user."""
        # Arrange
        user = UserFactory()
        connection1 = UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
        )
        connection2 = UserConnectionFactory(
            user=user,
            provider="polar",
            status=ConnectionStatus.ACTIVE,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert any(c["id"] == str(connection1.id) for c in data)
        assert any(c["id"] == str(connection2.id) for c in data)

    def test_get_connections_empty_list(self, client: TestClient, db: Session) -> None:
        """Test retrieving connections for a user with no connections."""
        # Arrange
        user = UserFactory()
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_get_connections_multiple_providers(self, client: TestClient, db: Session) -> None:
        """Test retrieving connections for multiple providers."""
        # Arrange
        user = UserFactory()
        providers = ["garmin", "polar", "suunto", "apple"]
        [UserConnectionFactory(user=user, provider=provider) for provider in providers]
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 4
        returned_providers = {c["provider"] for c in data}
        assert returned_providers == set(providers)

    def test_get_connections_different_statuses(self, client: TestClient, db: Session) -> None:
        """Test retrieving connections with different statuses."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
        )
        UserConnectionFactory(
            user=user,
            provider="polar",
            status=ConnectionStatus.REVOKED,
        )
        UserConnectionFactory(
            user=user,
            provider="suunto",
            status=ConnectionStatus.EXPIRED,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        statuses = {c["status"] for c in data}
        assert ConnectionStatus.ACTIVE.value in statuses
        assert ConnectionStatus.REVOKED.value in statuses
        assert ConnectionStatus.EXPIRED.value in statuses

    def test_get_connections_user_isolation(self, client: TestClient, db: Session) -> None:
        """Test that users can only see their own connections."""
        # Arrange
        user1 = UserFactory()
        user2 = UserFactory()
        connection1 = UserConnectionFactory(user=user1, provider="garmin")
        UserConnectionFactory(user=user2, provider="polar")
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act - get user1's connections
        response = client.get(f"/api/v1/users/{user1.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == str(connection1.id)
        assert data[0]["provider"] == "garmin"

    def test_get_connections_response_structure(self, client: TestClient, db: Session) -> None:
        """Test that response contains all expected fields."""
        # Arrange
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="garmin",
            provider_user_id="test_user_123",
            provider_username="test_user",
            status=ConnectionStatus.ACTIVE,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        connection_data = data[0]

        # Verify essential fields are present
        assert "id" in connection_data
        assert "user_id" in connection_data
        assert "provider" in connection_data
        assert "provider_user_id" in connection_data
        assert "provider_username" in connection_data
        assert "status" in connection_data
        assert "created_at" in connection_data
        assert "updated_at" in connection_data

        # Verify values
        assert connection_data["id"] == str(connection.id)
        assert connection_data["user_id"] == str(user.id)
        assert connection_data["provider"] == "garmin"
        assert connection_data["status"] == ConnectionStatus.ACTIVE.value

    def test_get_connections_missing_api_key(self, client: TestClient, db: Session) -> None:
        """Test that request without API key is rejected."""
        # Arrange
        user = UserFactory()

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections")

        # Assert
        assert response.status_code == 401

    def test_get_connections_invalid_api_key(self, client: TestClient, db: Session) -> None:
        """Test that request with invalid API key is rejected."""
        # Arrange
        user = UserFactory()
        headers = api_key_headers("invalid-api-key")

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 401

    def test_get_connections_invalid_user_id(self, client: TestClient, db: Session) -> None:
        """Test handling of invalid user ID format returns 400."""
        # Arrange
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act - FastAPI/Starlette validates UUID path params and returns 400 Bad Request
        response = client.get("/api/v1/users/not-a-uuid/connections", headers=headers)

        # Assert
        assert response.status_code == 400

    def test_get_connections_nonexistent_user(self, client: TestClient, db: Session) -> None:
        """Test retrieving connections for a user that doesn't exist."""
        # Arrange
        from uuid import uuid4

        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)
        nonexistent_user_id = uuid4()

        # Act
        response = client.get(f"/api/v1/users/{nonexistent_user_id}/connections", headers=headers)

        # Assert - should return empty list, not 404
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 0

    def test_get_connections_with_sync_metadata(self, client: TestClient, db: Session) -> None:
        """Test that connections include sync metadata."""
        # Arrange
        from datetime import datetime, timezone

        user = UserFactory()
        last_synced = datetime(2025, 12, 15, 12, 0, 0, tzinfo=timezone.utc)
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
            last_synced_at=last_synced,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert "last_synced_at" in data[0]
        # The last_synced_at should be present when set
        if data[0]["last_synced_at"]:
            assert isinstance(data[0]["last_synced_at"], str)

    def test_get_connections_excludes_sensitive_data(self, client: TestClient, db: Session) -> None:
        """Test that sensitive data like access tokens are not exposed."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            access_token="secret_access_token",
            refresh_token="secret_refresh_token",
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        connection_data = data[0]

        # Verify sensitive fields are not exposed
        assert "access_token" not in connection_data
        assert "refresh_token" not in connection_data


class TestDisconnectEndpoint:
    """Test suite for DELETE /api/v1/users/{user_id}/connections/{provider}."""

    def test_disconnect_active_connection(self, client: TestClient, db: Session) -> None:
        """Test disconnecting an active connection returns 204 and revokes it."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 204
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert conn.status == ConnectionStatus.REVOKED

    def test_disconnect_clears_tokens(self, client: TestClient, db: Session) -> None:
        """Test that disconnecting clears access_token, refresh_token, and token_expires_at."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
            access_token="secret_access",
            refresh_token="secret_refresh",
            token_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert conn.access_token is None
        assert conn.refresh_token is None
        assert conn.token_expires_at is None

    def test_disconnect_already_revoked_is_idempotent(self, client: TestClient, db: Session) -> None:
        """Test that disconnecting an already revoked connection returns 204."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.REVOKED,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 204

    def test_disconnect_nonexistent_connection(self, client: TestClient, db: Session) -> None:
        """Test that disconnecting a nonexistent connection returns 404."""
        # Arrange
        user = UserFactory()
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 404

    def test_disconnect_expired_connection(self, client: TestClient, db: Session) -> None:
        """Test that an expired connection gets revoked."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="polar",
            status=ConnectionStatus.EXPIRED,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/polar", headers=headers)

        # Assert
        assert response.status_code == 204
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="polar").one()
        assert conn.status == ConnectionStatus.REVOKED

    def test_disconnect_invalid_provider(self, client: TestClient, db: Session) -> None:
        """Test that an invalid provider name is rejected."""
        # Arrange
        user = UserFactory()
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/not_a_provider", headers=headers)

        # Assert - FastAPI returns 400 for invalid enum path params
        assert response.status_code == 400

    def test_disconnect_missing_api_key(self, client: TestClient, db: Session) -> None:
        """Test that request without API key is rejected."""
        # Arrange
        user = UserFactory()

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin")

        # Assert
        assert response.status_code == 401

    def test_disconnect_invalid_api_key(self, client: TestClient, db: Session) -> None:
        """Test that request with invalid API key is rejected."""
        # Arrange
        user = UserFactory()
        headers = api_key_headers("invalid-api-key")

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 401

    def test_disconnect_sdk_provider(self, client: TestClient, db: Session) -> None:
        """Test disconnecting an SDK provider (no tokens to clear)."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="apple",
            status=ConnectionStatus.ACTIVE,
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/apple", headers=headers)

        # Assert
        assert response.status_code == 204
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="apple").one()
        assert conn.status == ConnectionStatus.REVOKED

    def test_disconnect_user_isolation(self, client: TestClient, db: Session) -> None:
        """Test that disconnecting user1's provider doesn't affect user2."""
        # Arrange
        user1 = UserFactory()
        user2 = UserFactory()
        UserConnectionFactory(user=user1, provider="garmin", status=ConnectionStatus.ACTIVE)
        UserConnectionFactory(user=user2, provider="garmin", status=ConnectionStatus.ACTIVE)
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act - disconnect user1's garmin
        response = client.delete(f"/api/v1/users/{user1.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 204
        conn1 = db.query(UserConnection).filter_by(user_id=user1.id, provider="garmin").one()
        conn2 = db.query(UserConnection).filter_by(user_id=user2.id, provider="garmin").one()
        assert conn1.status == ConnectionStatus.REVOKED
        assert conn2.status == ConnectionStatus.ACTIVE


class TestDisconnectDeregistration:
    """Test suite for provider deregistration during disconnect."""

    @patch("httpx.delete")
    def test_disconnect_calls_garmin_deregistration(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        """Test that disconnecting Garmin calls the deregistration API before revoking."""
        # Arrange
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_httpx_delete.return_value = mock_response

        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
            access_token="garmin_access_token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 204
        mock_httpx_delete.assert_called_once_with(
            "https://apis.garmin.com/partner-gateway/rest/user/registration",
            headers={"Authorization": "Bearer garmin_access_token"},
            timeout=30.0,
        )
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert conn.status == ConnectionStatus.REVOKED
        assert conn.access_token is None

    @patch("httpx.delete")
    def test_disconnect_succeeds_when_deregistration_fails(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        """Test that disconnect still works when the provider deregistration API fails."""
        # Arrange
        mock_httpx_delete.side_effect = Exception("Network error")

        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
            access_token="garmin_access_token",
            token_expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert - disconnect still succeeds
        assert response.status_code == 204
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert conn.status == ConnectionStatus.REVOKED

    @patch("httpx.delete")
    def test_disconnect_skips_deregistration_when_no_token(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        """Test that deregistration is skipped when connection has no access token."""
        # Arrange
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
            access_token=None,
            refresh_token=None,
        )
        api_key = ApiKeyFactory()
        headers = api_key_headers(api_key.id)

        # Act
        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        # Assert
        assert response.status_code == 204
        mock_httpx_delete.assert_not_called()

    @patch("httpx.delete")
    def test_disconnect_refreshes_expired_token_before_deregistration(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        """An expired access token is refreshed first and the new token is used to deregister."""
        refresh_response = MagicMock()
        refresh_response.json.return_value = {
            "access_token": "fresh_access",
            "token_type": "Bearer",
            "expires_in": 86400,
            "refresh_token": "fresh_refresh",
        }
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            status=ConnectionStatus.ACTIVE,
            access_token="stale_access",
            refresh_token="old_refresh",
            token_expires_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        headers = api_key_headers(ApiKeyFactory().id)

        with patch("httpx.post", return_value=refresh_response) as mock_post:
            response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        assert response.status_code == 204
        assert mock_post.call_args.args[0] == GARMIN_TOKEN_URL
        assert mock_post.call_args.kwargs["data"]["refresh_token"] == "old_refresh"
        mock_httpx_delete.assert_called_once()
        assert mock_httpx_delete.call_args.kwargs["headers"] == {"Authorization": "Bearer fresh_access"}
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert conn.status == ConnectionStatus.REVOKED
        assert conn.access_token is None
        assert conn.refresh_token is None

    @patch("httpx.delete")
    def test_disconnect_refreshes_token_inside_margin(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        """A token with less than 10 minutes left counts as expired."""
        refresh_response = MagicMock()
        refresh_response.json.return_value = {
            "access_token": "fresh_access",
            "token_type": "Bearer",
            "expires_in": 86400,
        }
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            access_token="nearly_expired",
            token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=8),
        )
        headers = api_key_headers(ApiKeyFactory().id)

        with patch("httpx.post", return_value=refresh_response) as mock_post:
            client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        mock_post.assert_called_once()
        assert mock_httpx_delete.call_args.kwargs["headers"] == {"Authorization": "Bearer fresh_access"}

    @patch("httpx.delete")
    def test_disconnect_does_not_refresh_live_token(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            access_token="live_access",
            token_expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        headers = api_key_headers(ApiKeyFactory().id)

        with patch("httpx.post") as mock_post:
            client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        mock_post.assert_not_called()
        assert mock_httpx_delete.call_args.kwargs["headers"] == {"Authorization": "Bearer live_access"}

    @patch("httpx.delete")
    def test_disconnect_when_refresh_is_rejected_still_revokes(
        self, mock_httpx_delete: MagicMock, client: TestClient, db: Session
    ) -> None:
        """Refresh rejected: deregistration is still tried with the stored token and the connection is revoked."""
        rejected = MagicMock()
        rejected.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rejected", request=MagicMock(), response=MagicMock(status_code=400, text="invalid_grant")
        )
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="garmin",
            access_token="stale_access",
            refresh_token="dead_refresh",
            token_expires_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        headers = api_key_headers(ApiKeyFactory().id)

        with patch("httpx.post", return_value=rejected):
            response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        assert response.status_code == 204
        assert mock_httpx_delete.call_args.kwargs["headers"] == {"Authorization": "Bearer stale_access"}
        conn = db.query(UserConnection).filter_by(user_id=user.id, provider="garmin").one()
        assert conn.status == ConnectionStatus.REVOKED
        # Tokens are cleared even though the failed refresh had already marked it revoked.
        assert conn.access_token is None
        assert conn.refresh_token is None


class TestDisconnectDataDeletion:
    """Disconnecting a cloud provider queues deletion of that provider's data."""

    def test_cloud_disconnect_queues_deletion(
        self, client: TestClient, db: Session, provider_network: dict[str, MagicMock]
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", token_expires_at=datetime.now(timezone.utc) + timedelta(1))
        headers = api_key_headers(ApiKeyFactory().id)

        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        assert response.status_code == 204
        provider_network["schedule"].assert_called_once_with(user.id, "garmin", reason="disconnect")

    def test_sdk_disconnect_keeps_data(
        self, client: TestClient, db: Session, provider_network: dict[str, MagicMock]
    ) -> None:
        """SDK providers (e.g. Apple Health) have no OAuth: disconnecting never deletes their data."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="apple", access_token=None, refresh_token=None)
        headers = api_key_headers(ApiKeyFactory().id)

        response = client.delete(f"/api/v1/users/{user.id}/connections/apple", headers=headers)

        assert response.status_code == 204
        provider_network["schedule"].assert_not_called()

    def test_missing_connection_queues_nothing(
        self, client: TestClient, db: Session, provider_network: dict[str, MagicMock]
    ) -> None:
        user = UserFactory()
        headers = api_key_headers(ApiKeyFactory().id)

        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=headers)

        assert response.status_code == 404
        provider_network["schedule"].assert_not_called()
