from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.database import DbSession
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import AuthorizationURLResponse, OAuthState
from app.schemas.model_crud.data_priority import (
    BulkProviderSettingsUpdate,
    ProviderSettingRead,
    ProviderSettingUpdate,
)
from app.services import ApiKeyDep, DeveloperDep, user_connection_service, user_service
from app.services.provider_settings_service import ProviderSettingsService
from app.services.providers.base_strategy import BaseProviderStrategy
from app.services.providers.factory import ProviderFactory
from app.services.providers.templates.base_oauth import OAuthConnectionError
from app.utils.oauth_redirect import add_query_params, is_allowed_redirect_uri, short_reason
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

router = APIRouter()
factory = ProviderFactory()
settings_service = ProviderSettingsService()


def get_oauth_strategy(provider: ProviderName) -> BaseProviderStrategy:
    """Helper to get provider strategy and ensure it supports OAuth."""
    strategy = factory.get_provider(provider.value)

    if not strategy.oauth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider '{provider.value}' does not support OAuth",
        )
    return strategy


@router.get(
    "/{provider}/authorize",
    summary="Get Provider Authorization URL",
    status_code=status.HTTP_200_OK,
    response_model=AuthorizationURLResponse,
    tags=["External: Providers"],
)
def authorize_provider(
    provider: ProviderName,
    db: DbSession,
    _auth: ApiKeyDep,
    user_id: Annotated[UUID, Query(description="User ID to connect")],
    redirect_uri: Annotated[
        str | None,
        Query(
            description="Optional URI to send the user to after authorization. Must be under API_BASE_URL, "
            "FRONTEND_URL or one of OAUTH_ALLOWED_REDIRECT_PREFIXES."
        ),
    ] = None,
):
    """
    Initiate OAuth flow for a provider.

    Requires an API key or a developer session. The user must exist (404 otherwise)
    and redirect_uri, if given, must be on the allow-list (400 otherwise).

    Returns authorization URL where user should be redirected to log in.
    """
    strategy = get_oauth_strategy(provider)

    if redirect_uri is not None and not is_allowed_redirect_uri(redirect_uri):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="redirect_uri is not allowed",
        )
    user_service.get(db, user_id, raise_404=True)

    assert strategy.oauth
    auth_url, state = strategy.oauth.get_authorization_url(user_id, redirect_uri)
    return AuthorizationURLResponse(authorization_url=auth_url, state=state)


@router.get("/{provider}/callback", tags=["System: OAuth"])
def oauth_callback(
    provider: ProviderName,
    db: DbSession,
    code: Annotated[str | None, Query(description="Authorization code from provider")] = None,
    state: Annotated[str | None, Query(description="State parameter for CSRF protection")] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
):
    """
    OAuth callback endpoint.

    Provider redirects here after user authorizes. Exchanges code for tokens.

    When the flow was started with a redirect_uri, the user is sent back to it
    on success (with ``status=success&provider=<provider>``) and on failure
    (with ``status=error&error=<reason>``). Without one, the internal
    success/error pages are used.
    """
    strategy = get_oauth_strategy(provider)
    assert strategy.oauth

    # Consume the state up front so failures can still go back to the client's redirect_uri.
    oauth_state: OAuthState | None = None
    code_verifier: str | None = None
    state_error: HTTPException | None = None
    if state:
        try:
            oauth_state, code_verifier = strategy.oauth.consume_state(state)
        except HTTPException as e:
            state_error = e

    redirect_uri = oauth_state.redirect_uri if oauth_state else None

    if error:
        if redirect_uri:
            return _error_redirect(redirect_uri, provider, short_reason(error, default="provider_error"))
        return RedirectResponse(
            url=f"/api/v1/oauth/error?message={error}:+{error_description or 'Unknown+error'}",
            status_code=303,
        )

    if not code or not state:
        if redirect_uri:
            return _error_redirect(redirect_uri, provider, "missing_oauth_parameters")
        return RedirectResponse(
            url="/api/v1/oauth/error?message=Missing+OAuth+parameters",
            status_code=303,
        )

    if oauth_state is None:
        # Invalid or expired state: there is no redirect_uri to send the user back to.
        raise state_error or HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state")

    try:
        oauth_state = strategy.oauth.complete_callback(db, code, oauth_state, code_verifier)
    except Exception as e:
        if not redirect_uri:
            raise
        reason = e.reason if isinstance(e, OAuthConnectionError) else "connection_failed"
        log_structured(
            logger,
            "warning",
            f"OAuth callback failed, sending user back to redirect_uri: {e}",
            provider=provider.value,
            task="oauth_callback",
            user_id=str(oauth_state.user_id),
            reason=reason,
        )
        return _error_redirect(redirect_uri, provider, reason)

    # Stamp last_synced_at=now so the first periodic sync uses the connection
    # timestamp as its live-sync cursor and won't attempt to pull all history.
    user_connection_service.stamp_last_synced_at(db, oauth_state.user_id, provider.value)

    # Grace-period flag: automatically kick off a historical sync so integrators
    # who haven't yet adopted the explicit /sync/historical call still get backfill.
    # Controlled by HISTORICAL_SYNC_ON_CONNECT (default: true).
    # HISTORICAL_SYNC_DISABLED_PROVIDERS opts single providers out (e.g. a push-only
    # Garmin app without backfill access) while the others keep it.
    if settings.historical_sync_on_connect and provider.value not in settings.historical_sync_disabled_providers:
        caps = strategy.capabilities
        if caps.webhook_callback:
            # this code is going to be removed later, so leave inner imports heres
            from app.integrations.celery.tasks import start_garmin_full_backfill

            start_garmin_full_backfill.delay(str(oauth_state.user_id))
        elif caps.rest_pull:
            from app.integrations.celery.tasks import sync_vendor_data

            now = datetime.now(timezone.utc)
            start_date = (now - timedelta(days=90)).isoformat()
            sync_vendor_data.delay(
                user_id=str(oauth_state.user_id),
                start_date=start_date,
                end_date=now.isoformat(),
                providers=[provider.value],
                is_historical=True,
            )

    # If a specific redirect_uri was requested (e.g. by frontend), redirect there
    if oauth_state.redirect_uri:
        success_url = add_query_params(
            oauth_state.redirect_uri,
            {"status": "success", "provider": provider.value},
            override=False,
        )
        return RedirectResponse(url=success_url, status_code=303)

    # Otherwise, redirect to internal success page
    return RedirectResponse(
        url=f"/api/v1/oauth/success?provider={provider.value}&user_id={oauth_state.user_id}",
        status_code=303,
    )


def _error_redirect(redirect_uri: str, provider: ProviderName, reason: str) -> RedirectResponse:
    """Send the user back to the client's redirect_uri with status=error and a short reason."""
    url = add_query_params(redirect_uri, {"status": "error", "error": reason}, override=True)
    url = add_query_params(url, {"provider": provider.value}, override=False)
    return RedirectResponse(url=url, status_code=303)


@router.get("/success", tags=["System: OAuth"])
def oauth_success(
    provider: Annotated[str, Query()],
    user_id: Annotated[str, Query()],
) -> dict:
    """Simple success page after OAuth completion."""
    return {
        "success": True,
        "message": f"Successfully connected to {provider}",
        "user_id": user_id,
        "provider": provider,
    }


@router.get("/error", tags=["System: OAuth"])
def oauth_error(
    message: Annotated[str, Query()] = "OAuth authentication failed",
) -> dict:
    """OAuth error page."""
    return {
        "success": False,
        "message": message,
    }


@router.get("/providers", response_model=list[ProviderSettingRead], tags=["External: Providers"])
def get_providers(
    db: DbSession,
    enabled_only: Annotated[bool, Query(description="Return only enabled providers")] = False,
    cloud_only: Annotated[bool, Query(description="Return only cloud (OAuth) providers")] = False,
):
    """
    Get providers with their configuration and metadata.

    Query params:
    - enabled_only: Filter to only enabled providers (default: False, returns all)
    - cloud_only: Filter to only providers with cloud OAuth API (default: False)

    Returns full provider details including name, icon_url, has_cloud_api, is_enabled.
    """
    all_providers = settings_service.get_all_providers(db)

    return [p for p in all_providers if (not enabled_only or p.is_enabled) and (not cloud_only or p.has_cloud_api)]


@router.put("/providers/{provider}", response_model=ProviderSettingRead, tags=["Internal: Providers"])
def update_provider_setting(
    provider: str,
    update: ProviderSettingUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """Update is_enabled and/or live_sync_mode for a single provider."""
    try:
        return settings_service.update_provider_setting(db, provider, update)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.put("/providers", response_model=list[ProviderSettingRead], tags=["Internal: Providers"])
def bulk_update_providers(
    updates: BulkProviderSettingsUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """
    Bulk update provider settings.

    Accepts a map of provider_id -> is_enabled and updates all providers at once.
    This is the primary endpoint for the admin UI to save checkbox states.
    """
    return settings_service.bulk_update_providers(db, updates.providers)
