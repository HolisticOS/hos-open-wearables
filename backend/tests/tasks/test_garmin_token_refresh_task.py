"""Tests for the daily Garmin token refresh that keeps push-only refresh tokens alive."""

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.garmin.token_refresh_task import TASK_NAME, refresh_garmin_tokens
from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from tests.factories import UserConnectionFactory

MODULE = "app.integrations.celery.tasks.garmin.token_refresh_task"
TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"


@pytest.fixture
def task_db(db: Session) -> Generator[Session, None, None]:
    session_cm = MagicMock()
    session_cm.__enter__.return_value = db
    with patch(f"{MODULE}.SessionLocal", return_value=session_cm):
        yield db


def _token_response(access: str, refresh: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 86400,
        "refresh_token": refresh,
    }
    return response


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestRefreshGarminTokens:
    def test_refreshes_expired_and_soon_expiring_garmin_tokens(self, task_db: Session) -> None:
        expired = UserConnectionFactory(
            provider="garmin", refresh_token="r1", token_expires_at=_now() - timedelta(days=3)
        )
        soon = UserConnectionFactory(
            provider="garmin", refresh_token="r2", token_expires_at=_now() + timedelta(hours=20)
        )

        with patch("httpx.post", side_effect=[_token_response("a1", "n1"), _token_response("a2", "n2")]) as post:
            result = refresh_garmin_tokens.run()

        assert result == {"refreshed": 2, "failed": 0, "candidates": 2}
        assert {c.kwargs["data"]["refresh_token"] for c in post.call_args_list} == {"r1", "r2"}
        assert all(c.args[0] == TOKEN_URL for c in post.call_args_list)
        task_db.refresh(expired)
        task_db.refresh(soon)
        # The refresh token rotates, so it never reaches its own expiry.
        assert {expired.refresh_token, soon.refresh_token} == {"n1", "n2"}
        assert expired.token_expires_at > _now() + timedelta(hours=23)

    def test_skips_fresh_revoked_tokenless_and_other_providers(self, task_db: Session) -> None:
        UserConnectionFactory(provider="garmin", token_expires_at=_now() + timedelta(days=2))
        UserConnectionFactory(provider="garmin", status=ConnectionStatus.REVOKED, token_expires_at=_now())
        UserConnectionFactory(provider="garmin", refresh_token=None, token_expires_at=_now())
        UserConnectionFactory(provider="polar", token_expires_at=_now())

        with patch("httpx.post") as post:
            result = refresh_garmin_tokens.run()

        assert result == {"refreshed": 0, "failed": 0, "candidates": 0}
        post.assert_not_called()

    def test_dead_refresh_token_revokes_and_continues(self, task_db: Session) -> None:
        rejected = MagicMock()
        rejected.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rejected", request=MagicMock(), response=MagicMock(status_code=400, text="invalid_grant")
        )
        dead = UserConnectionFactory(provider="garmin", refresh_token="dead", token_expires_at=_now() - timedelta(1))
        alive = UserConnectionFactory(provider="garmin", refresh_token="ok", token_expires_at=_now() - timedelta(1))

        def respond(url: str, data: dict, **_: object) -> MagicMock:
            return rejected if data["refresh_token"] == "dead" else _token_response("a", "n")

        with patch("httpx.post", side_effect=respond):
            result = refresh_garmin_tokens.run()

        assert result == {"refreshed": 1, "failed": 1, "candidates": 2}
        task_db.expire_all()
        assert task_db.get(UserConnection, dead.id).status == ConnectionStatus.REVOKED
        assert task_db.get(UserConnection, alive.id).refresh_token == "n"


class TestBeatSchedule:
    def test_daily_entry_routed_to_garmin_queue(self) -> None:
        with patch("app.integrations.celery.core.current_celery_app") as app:
            create_celery()

        entry = app.conf.beat_schedule["refresh-garmin-tokens-daily"]
        assert entry["task"] == TASK_NAME
        routes = app.conf.update.call_args.kwargs["task_routes"]
        assert routes[TASK_NAME] == {"queue": "garmin_sync"}
        assert "garmin_sync" in app.conf.update.call_args.kwargs["task_queues"]
