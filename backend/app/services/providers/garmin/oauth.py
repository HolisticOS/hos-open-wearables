import logging

import httpx
from starlette.status import HTTP_502_BAD_GATEWAY

from app.config import settings
from app.schemas.auth import (
    AuthenticationMethod,
)
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate, OAuthConnectionError
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class GarminOAuth(BaseOAuthTemplate):
    """Garmin OAuth 2.0 with PKCE implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://connect.garmin.com/oauth2Confirm",
            token_url="https://diauth.garmin.com/di-oauth2-service/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.garmin_client_id or "",
            client_secret=(settings.garmin_client_secret.get_secret_value() if settings.garmin_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.GARMIN),
            default_scope=settings.garmin_default_scope,
        )

    use_pkce = True
    auth_method = AuthenticationMethod.BODY
    # Garmin pushes data per Garmin account. Linking one account to several OW
    # users would duplicate every push, so the newest connection wins.
    single_owner_per_account = True
    # Garmin pushes data whatever our tokens, and tells us itself when a user
    # disconnects (deregistration / permissions push). A rejected refresh only
    # costs FIT files and backfills, so it must not revoke: it used to, on every
    # new activity's FIT download, dropping every push after it (2026-10-28).
    revoke_on_refresh_reject = False

    def deregister_user(self, access_token: str) -> None:
        """Call Garmin's user deregistration endpoint to remove the app association."""
        response = httpx.delete(
            f"{self.api_base_url}/partner-gateway/rest/user/registration",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        response.raise_for_status()

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Fetches Garmin user ID and API permissions.

        The Garmin user ID is required: pushes are matched to a connection by it,
        so a connection saved without it would silently drop every push. If it
        cannot be fetched the connection fails (OAuthConnectionError) instead.
        """
        provider_user_id: str | None = None
        try:
            user_id_response = httpx.get(
                f"{self.api_base_url}/partner-gateway/rest/user/id",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            user_id_response.raise_for_status()
            raw_user_id = user_id_response.json().get("userId")
            provider_user_id = str(raw_user_id) if raw_user_id else None
            failure = None if provider_user_id else "response had no user id"
        except Exception as e:
            failure = str(e)

        if not provider_user_id:
            log_structured(
                logger,
                "error",
                f"Garmin user id lookup failed, connection not saved: {failure}",
                provider="garmin",
                task="get_provider_user_info",
                user_id=user_id,
            )
            raise OAuthConnectionError(
                "provider_user_lookup_failed",
                "Could not fetch the Garmin user id; the connection was not saved",
                status_code=HTTP_502_BAD_GATEWAY,
            )

        # Fetch permissions (best-effort - fail returns scope as None)
        scope: str | None = None
        try:
            permissions_response = httpx.get(
                f"{self.api_base_url}/partner-gateway/rest/user/permissions",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            permissions_response.raise_for_status()
            data = permissions_response.json()
            # Response format: {"permissions": ["ACTIVITY_EXPORT", "HEALTH_EXPORT", ...]}
            permissions = data.get("permissions", [])
            if permissions:
                scope = " ".join(sorted(permissions))
        except Exception:
            logger.warning(
                "Failed to fetch Garmin permissions for user %s, will retry via webhook",
                provider_user_id,
                exc_info=True,
            )

        return {"user_id": provider_user_id, "username": None, "scope": scope}
