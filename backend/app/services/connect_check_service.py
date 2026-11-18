"""Connect check: a shareable page where one person connects a provider and watches their data arrive.

For trying a new integration with someone outside the team (an Amazfit owner for
Zepp): a developer mints a link with an API key, the person opens it, connects,
and the page shows the connection and the last 30 days of sleep, workouts and
HRV/SpO2/resting HR from that provider as they come in.

The link is the credential: ``base64url(user_id.provider.expiry).signature``,
signed with a key derived from SECRET_KEY. It scopes the page to one OW user and
one provider, expires (14 days by default), and shows nothing but that user's
own data for that provider.
"""

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.config import settings
from app.database import DbSession
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import EventRecordQueryParams, TimeSeriesQueryParams
from app.schemas.model_crud.user_management import UserCreate
from app.services.event_record_service import event_record_service
from app.services.providers.factory import ProviderFactory
from app.services.timeseries_service import timeseries_service
from app.services.user_connection_service import user_connection_service
from app.services.user_service import user_service
from app.utils.config_utils import derive_secret
from app.utils.exceptions import InvalidInputError, NotFoundError

LINK_TTL = timedelta(days=14)
WINDOW = timedelta(days=30)
SERIES = [
    SeriesType.heart_rate_variability_rmssd,
    SeriesType.oxygen_saturation,
    SeriesType.resting_heart_rate,
    SeriesType.steps,
]


def _sign(payload: str) -> str:
    key = derive_secret(settings.secret_key, "connect-check-link").encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(user_id: UUID, provider: str, expires_at: datetime) -> str:
    payload = f"{user_id}.{provider}.{int(expires_at.timestamp())}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def read_token(token: str, now: datetime | None = None) -> tuple[UUID, str]:
    """(user_id, provider) of a valid, unexpired link. Anything else is a 404, revealing nothing."""
    try:
        encoded, signature = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        user_id, provider, expiry = payload.split(".")
        valid = hmac.compare_digest(signature, _sign(payload))
        expired = datetime.fromtimestamp(int(expiry), tz=timezone.utc) < (now or datetime.now(timezone.utc))
        if valid and not expired:
            return UUID(user_id), provider
    except (ValueError, UnicodeDecodeError):
        pass
    raise NotFoundError("Not Found")


class ConnectCheckService:
    def _oauth(self, provider: str) -> Any:
        try:
            oauth = ProviderFactory().get_provider(ProviderName(provider).value).oauth
        except ValueError:
            oauth = None
        if oauth is None:
            raise InvalidInputError(f"{provider} has no OAuth connect")
        return oauth

    def page_url(self, token: str) -> str:
        return f"{settings.api_base_url}{settings.api_v1}/connect-check/{token}"

    def create_link(self, db: DbSession, provider: str, label: str | None) -> dict[str, Any]:
        """A new OW user for the tester and a link for them."""
        self._oauth(provider)
        user = user_service.create(db, UserCreate(first_name=label or f"{provider} connect check"))
        expires_at = datetime.now(timezone.utc) + LINK_TTL
        token = make_token(user.id, provider, expires_at)
        return {
            "url": self.page_url(token),
            "user_id": str(user.id),
            "provider": provider,
            "expires_at": expires_at.isoformat(),
        }

    def authorization_url(self, user_id: UUID, provider: str, token: str) -> str:
        url, _state = self._oauth(provider).get_authorization_url(user_id, self.page_url(token))
        return url

    def disconnect(self, db: DbSession, user_id: UUID, provider: str) -> None:
        user_connection_service.disconnect(db, user_id, provider, oauth=self._oauth(provider))

    def snapshot(self, db: DbSession, user_id: UUID, provider: str) -> dict[str, Any]:
        """The connection and this provider's last 30 days for the page, newest first."""
        connection = user_connection_service.get_connection(db, user_id, provider)
        now = datetime.now(timezone.utc)
        since = now - WINDOW
        source = ProviderName(provider)

        def events(fetch: Any) -> list[dict[str, Any]]:
            params = EventRecordQueryParams(start_datetime=since, end_datetime=now, provider=source, limit=30)
            page = fetch(db, user_id, params)
            items = [item.model_dump(mode="json", exclude={"source"}) for item in page.data]
            return sorted(items, key=lambda i: i["start_time"], reverse=True)

        samples = timeseries_service.get_timeseries(
            db,
            user_id,
            SERIES,
            TimeSeriesQueryParams(start_datetime=since, end_datetime=now, provider=source, limit=200),
        )
        series: dict[str, list[dict[str, Any]]] = {}
        for sample in samples.data:
            series.setdefault(sample.type.value, []).append(
                {"timestamp": sample.timestamp.isoformat(), "value": sample.value, "unit": sample.unit}
            )
        for values in series.values():
            values.sort(key=lambda v: v["timestamp"], reverse=True)

        return {
            "provider": provider,
            "connection": {
                # The ORM hands the status back as a plain str.
                "status": ConnectionStatus(connection.status).value if connection else None,
                "connected": bool(connection and connection.status == ConnectionStatus.ACTIVE),
                "last_synced_at": connection.last_synced_at.isoformat()
                if connection and connection.last_synced_at
                else None,
                "updated_at": connection.updated_at.isoformat() if connection else None,
            },
            "sleep": events(event_record_service.get_sleep_sessions),
            "workouts": events(event_record_service.get_workouts),
            "series": series,
            "generated_at": now.isoformat(),
        }


connect_check_service = ConnectCheckService()
