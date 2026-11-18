"""Zepp (Amazfit) OAuth 2.0 — https://devopen.zepp.com/docs/authentication/oauth2-overview

How Zepp differs from a textbook OAuth 2.0 provider:

- The authorize page is a hash route (``index.html#/login?...``) and takes no
  ``redirect_uri`` or ``scope``: both are fixed when the app is registered.
  ``token=["access","refresh"]`` is what makes Zepp issue a refresh token.
- The code exchange posts the client credentials in the body and returns the
  Zepp ``user_id`` next to the tokens.
- Refresh has its own URL and wants the *refresh* token as the bearer.
- Revoking is ``POST /users/-/thirdparty/unauthorize`` with the access token.
"""

from urllib.parse import quote, urlencode
from uuid import UUID

import httpx
from starlette.status import HTTP_502_BAD_GATEWAY

from app.config import settings
from app.schemas.auth import AuthenticationMethod
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import OAuthTokenResponse, ProviderCredentials, ProviderEndpoints
from app.services.providers.templates.base_oauth import BaseOAuthTemplate, OAuthConnectionError
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

AUTHORIZE_URL = "https://user.zepp.com/oauth2/index.html"
TOKEN_URL = "https://auth.zepp.com/v2/oauth2/access_token"
REFRESH_URL = "https://auth.zepp.com/v2/oauth2/refresh_token"


class ZeppOAuth(BaseOAuthTemplate):
    """Zepp Open Platform OAuth (authorization code, client secret in the body, no PKCE)."""

    use_pkce = False
    auth_method = AuthenticationMethod.BODY
    # Pushes are keyed by the Zepp user id: one Zepp account, one OW user (latest wins).
    single_owner_per_account = True
    # Zepp pushes sleep/HRV whatever our tokens; a rejected refresh only costs FIT files.
    revoke_on_refresh_reject = False

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL, refresh_url=REFRESH_URL)

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.zepp_client_id or "",
            client_secret=settings.zepp_client_secret.get_secret_value() if settings.zepp_client_secret else "",
            redirect_uri=settings.oauth_redirect_uri(ProviderName.ZEPP),
            default_scope="",  # fixed at app registration; the authorize URL takes none
        )

    def _build_auth_url(self, state: str) -> tuple[str, None]:
        params = {
            "client_id": self.credentials.client_id,
            "response_type": "code",
            "state": state,
            "token": '["access","refresh"]',
        }
        return f"{self.endpoints.authorize_url}#/login?{urlencode(params, quote_via=quote)}", None

    def _prepare_token_request(self, code: str, code_verifier: str | None) -> tuple[dict, dict]:
        data, _ = super()._prepare_token_request(code, code_verifier)
        return data, {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}

    def _prepare_refresh_request(self, refresh_token: str) -> tuple[dict, dict]:
        data, _ = super()._prepare_refresh_request(refresh_token)
        return data, {
            "Authorization": f"Bearer {refresh_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """The Zepp user id, from the token response or else the profile.

        Required: every push is matched to a connection by it, so a connection
        saved without it would silently drop all of the member's data.
        """
        extra = token_response.model_extra or {}
        zepp_user_id = extra.get("user_id") or extra.get("userId")
        if not zepp_user_id:
            try:
                response = httpx.get(
                    f"{self.api_base_url}/users/-/profile",
                    headers={"Authorization": f"Bearer {token_response.access_token}"},
                    timeout=30.0,
                )
                response.raise_for_status()
                zepp_user_id = response.json().get("userId")
            except Exception as e:
                logger.warning("Zepp profile lookup failed", provider="zepp", user_id=user_id, exc_info=e)
        if not zepp_user_id:
            raise OAuthConnectionError(
                "provider_user_lookup_failed",
                "Could not read the Zepp user id; the connection was not saved",
                status_code=HTTP_502_BAD_GATEWAY,
            )
        return {"user_id": str(zepp_user_id), "username": None}

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        response = httpx.post(
            f"{self.api_base_url}/users/-/thirdparty/unauthorize",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        response.raise_for_status()

    def request_backfill(self, access_token: str, user_id: UUID, start_s: int, end_s: int) -> dict[str, int]:
        """Ask Zepp to push history for [start_s, end_s] (≤ 30 days). Answers arrive as webhooks.

        Returns the HTTP status per data type; 429 means the app's 50 requests/hour are spent.
        """
        results: dict[str, int] = {}
        for path in ("sleeps", "hrv", "activities"):
            try:
                response = httpx.get(
                    f"{self.api_base_url}/wellness-api/rest/backfill/{path}",
                    params={"summaryStartTimeInSeconds": start_s, "summaryEndTimeInSeconds": end_s},
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0,
                )
                results[path] = response.status_code
            except httpx.RequestError as e:
                logger.warning("Zepp backfill request failed", provider="zepp", user_id=str(user_id), exc_info=e)
                results[path] = 0
        return results
