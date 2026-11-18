"""Zepp (Amazfit): OAuth quirks, the array-of-JSON-strings push body, and what a push stores."""

import json
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import EventRecord
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import OAuthTokenResponse
from app.services.fit_parser import parse_fit_file
from app.services.providers.factory import ProviderFactory
from app.services.providers.templates.base_oauth import OAuthConnectionError
from app.services.providers.zepp.data_247 import Zepp247Data, with_identity
from app.services.providers.zepp.oauth import REFRESH_URL, ZeppOAuth
from app.services.providers.zepp.strategy import ZeppStrategy
from app.services.providers.zepp.webhook_handler import AUTH_CHANGE, ZeppWebhookHandler, parse_zepp_body
from app.utils.exceptions import InvalidInputError
from tests.factories import UserConnectionFactory, UserFactory
from tests.fixtures.fit_builder import make_running_session_fit

ZEPP_USER_ID = "300123456"
NIGHT_START = 1763348400  # 2025-11-17 03:00 UTC
DATA_247 = "app.services.providers.zepp.data_247"

SLEEP: dict[str, Any] = {
    "userId": ZEPP_USER_ID,
    "summaryId": None,
    "calendarDate": "2025-11-17",
    "startTimeInSeconds": NIGHT_START,
    "startTimeOffsetInSeconds": -18000,
    "durationInSeconds": 28800,
    "deepSleepDurationInSeconds": 5400,
    "lightSleepDurationInSeconds": 14400,
    "remSleepInSeconds": 5400,
    "awakeDurationInSeconds": 3600,
    "overallSleepScore": {"value": 82, "qualifierKey": "GOOD"},
    "sleepLevelsMap": {
        "deep": [{"startTimeInSeconds": NIGHT_START, "endTimeInSeconds": NIGHT_START + 5400}],
        "light": [{"startTimeInSeconds": NIGHT_START + 5400, "endTimeInSeconds": NIGHT_START + 28800}],
    },
}
HRV: dict[str, Any] = {
    "userId": ZEPP_USER_ID,
    "summaryId": None,
    "calendarDate": "2025-11-17",
    "startTimeInSeconds": NIGHT_START,
    "startTimeOffsetInSeconds": -18000,
    "lastNightAvg": 48,
    "hrvValues": {"300": 46, "600": 50},
}
ACTIVITY: dict[str, Any] = {
    "userId": ZEPP_USER_ID,
    "summaryId": "act-9",
    "fileType": "FIT",
    "callbackURL": "https://api-open.zepp.com/api/activity/file?id=1&token=t",
    "activityType": "RUNNING",
    "deviceName": "Amazfit T-Rex 3",
    "startTimeInSeconds": 1748764800,  # 2025-06-01 08:00 UTC, the fixture's start
    "activityName": "Outdoor Run",
}


def zepp_body(*objects: dict[str, Any]) -> bytes:
    """What Zepp sends: a JSON array of JSON *strings*."""
    return json.dumps([json.dumps(o) for o in objects]).encode()


@pytest.fixture
def oauth() -> ZeppOAuth:
    return ZeppOAuth(
        user_repo=MagicMock(),
        connection_repo=UserConnectionRepository(),
        provider_name="zepp",
        api_base_url="https://api-open.zepp.com",
    )


def zepp_247() -> Zepp247Data:
    data_247 = ZeppStrategy().data_247
    assert isinstance(data_247, Zepp247Data)
    return data_247


@pytest.fixture
def handler() -> ZeppWebhookHandler:
    webhooks = ZeppStrategy().webhooks
    assert isinstance(webhooks, ZeppWebhookHandler)
    return webhooks


class TestParseBody:
    def test_array_of_json_strings_is_merged_by_key(self) -> None:
        merged = parse_zepp_body(zepp_body({"sleeps": [SLEEP]}, {"hrv": [HRV]}, {"sleeps": [SLEEP]}))
        assert {k: len(v) for k, v in merged.items()} == {"sleeps": 2, "hrv": 1}

    def test_plain_objects_and_a_bad_element_are_tolerated(self) -> None:
        assert parse_zepp_body(json.dumps({"hrv": [HRV]}).encode()) == {"hrv": [HRV]}
        assert parse_zepp_body(json.dumps([{"hrv": [HRV]}, "not json"]).encode()) == {"hrv": [HRV]}

    def test_authorization_change_is_collected(self) -> None:
        event = {"userId": ZEPP_USER_ID, "eventType": AUTH_CHANGE, "changeType": "delete", "eventTime": 1}
        assert parse_zepp_body(zepp_body(event)) == {AUTH_CHANGE: [event]}

    def test_garbage_is_a_400(self) -> None:
        with pytest.raises(InvalidInputError):
            parse_zepp_body(b"<html>")


class TestIdentity:
    def test_null_summary_id_gets_zepps_documented_key(self) -> None:
        assert with_identity("sleeps", SLEEP)["summaryId"] == f"sleeps-2025-11-17-{NIGHT_START}"
        assert with_identity("sleeps", {**SLEEP, "summaryId": "s1"})["summaryId"] == "s1"

    def test_pulse_ox_values_get_garmins_name(self) -> None:
        item = with_identity("pulseox", {"summaryId": "p", "timeOffsetPulseOxValues": {"0": 97}})
        assert item["timeOffsetSpo2Values"] == {"0": 97}


class TestOAuth:
    def test_authorize_url_is_the_hash_route_asking_for_a_refresh_token(self, oauth: ZeppOAuth) -> None:
        url, pkce = oauth._build_auth_url("st8")
        assert pkce is None
        assert url.startswith("https://user.zepp.com/oauth2/index.html#/login?")
        assert "response_type=code" in url
        assert "state=st8" in url
        assert "token=%5B%22access%22%2C%22refresh%22%5D" in url
        assert "redirect_uri" not in url

    def test_refresh_goes_to_its_own_url_with_the_refresh_token_as_bearer(self, oauth: ZeppOAuth) -> None:
        data, headers = oauth._prepare_refresh_request("rt-1")
        assert oauth.endpoints.refresh_url == REFRESH_URL
        assert headers["Authorization"] == "Bearer rt-1"
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "rt-1"
        assert "client_secret" in data

    def test_refresh_posts_to_the_refresh_url(self, db: Session, oauth: ZeppOAuth) -> None:
        response = MagicMock()
        response.json.return_value = {"access_token": "new", "token_type": "Bearer", "expires_in": 3600}
        with patch("app.services.providers.templates.base_oauth.httpx.post", return_value=response) as post:
            oauth.refresh_access_token(db, uuid4(), "rt-1")
        assert post.call_args.args[0] == REFRESH_URL

    def test_user_id_comes_from_the_token_response(self, oauth: ZeppOAuth) -> None:
        token = OAuthTokenResponse.model_validate(
            {"access_token": "a", "token_type": "Bearer", "expires_in": 3600, "user_id": 300123}
        )
        assert oauth._get_provider_user_info(token, "u")["user_id"] == "300123"

    def test_user_id_falls_back_to_the_profile(self, oauth: ZeppOAuth) -> None:
        token = OAuthTokenResponse.model_validate({"access_token": "a", "token_type": "Bearer", "expires_in": 3600})
        response = MagicMock()
        response.json.return_value = {"userId": "777"}
        with patch("app.services.providers.zepp.oauth.httpx.get", return_value=response):
            assert oauth._get_provider_user_info(token, "u")["user_id"] == "777"

    def test_no_user_id_fails_the_connection(self, oauth: ZeppOAuth) -> None:
        token = OAuthTokenResponse.model_validate({"access_token": "a", "token_type": "Bearer", "expires_in": 3600})
        with (
            patch("app.services.providers.zepp.oauth.httpx.get", side_effect=httpx.ConnectError("down")),
            pytest.raises(OAuthConnectionError),
        ):
            oauth._get_provider_user_info(token, "u")


class TestStrategy:
    def test_factory_and_capabilities(self) -> None:
        strategy = ProviderFactory().get_provider("zepp")
        assert isinstance(strategy, ZeppStrategy)
        assert strategy.capabilities.webhook_stream
        assert strategy.capabilities.webhook_callback
        assert strategy.icon_url == "/static/provider-icons/zepp.svg"
        assert strategy.webhooks is not None
        assert not strategy.webhooks.requires_callback_token

    def test_historical_sync_queues_the_backfill_request(self) -> None:
        with patch("app.services.providers.zepp.strategy.celery_app") as celery:
            result = ZeppStrategy().start_historical_sync(uuid4(), 90)
        assert result.days == 30
        assert result.method == "webhook_backfill"
        assert celery.send_task.call_args.args[0].endswith("zepp.backfill_task.request_backfill")


class TestWorkoutFromFit:
    def test_session_totals_fill_the_workout(self) -> None:
        user_id = uuid4()
        fit = parse_fit_file(make_running_session_fit(), user_id, source="zepp")
        built = zepp_247().build_fit_workout(user_id, ACTIVITY, fit)
        assert built is not None
        record, detail = built
        assert (record.end_datetime - record.start_datetime).total_seconds() == 1800
        assert record.device_model == "Amazfit T-Rex 3"
        assert record.type == "running"
        assert detail.heart_rate_avg == 151
        assert detail.heart_rate_max == 178
        assert detail.distance == 5000
        assert detail.energy_burned == 420
        assert detail.moving_time_seconds == 1750

    def test_no_session_no_workout(self) -> None:
        fit = parse_fit_file(make_running_session_fit(), uuid4())
        fit.session = None
        assert zepp_247().build_fit_workout(uuid4(), ACTIVITY, fit) is None


class TestProcessPayload:
    def test_sleep_and_hrv_are_stored_as_zepp(self, db: Session, handler: ZeppWebhookHandler) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="zepp", provider_user_id=ZEPP_USER_ID)

        result = handler.process_payload(db, {"sleeps": [SLEEP], "hrv": [HRV]}, "t1")

        assert result["errors"] == []
        assert result["synced_user_ids"] == [str(user.id)]
        assert result["results"]["hrv"]["saved"] >= 1
        sleep = db.query(EventRecord).filter(EventRecord.category == "sleep").one()
        assert sleep.source_name == "Zepp"
        assert sleep.external_id == f"sleeps-2025-11-17-{NIGHT_START}"

    def test_an_unknown_zepp_user_stores_nothing(self, db: Session, handler: ZeppWebhookHandler) -> None:
        result = handler.process_payload(db, {"sleeps": [SLEEP]}, "t2")
        assert result["synced_user_ids"] == []
        assert db.query(EventRecord).count() == 0

    def test_an_activity_becomes_a_workout_from_its_fit_file(self, db: Session, handler: ZeppWebhookHandler) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="zepp", provider_user_id=ZEPP_USER_ID)
        with (
            patch(f"{DATA_247}.download_binary_content", return_value=make_running_session_fit()) as download,
            patch(f"{DATA_247}.store_fit_file"),
        ):
            handler.process_payload(db, {"activityFiles": [ACTIVITY]}, "t3")
            handler.process_payload(db, {"activityFiles": [ACTIVITY]}, "t3b")  # Zepp re-sends

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").one()
        assert workout.source_name == "Zepp"
        assert workout.external_id == "act-9"
        assert workout.duration_seconds == 1800
        assert download.call_count == 1  # the one-time link isn't spent twice

    def test_authorization_change_revokes_the_connection(self, db: Session, handler: ZeppWebhookHandler) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="zepp", provider_user_id=ZEPP_USER_ID)
        event = {"userId": ZEPP_USER_ID, "eventType": AUTH_CHANGE, "changeType": "delete"}
        with patch("app.services.providers.zepp.webhook_handler.schedule_provider_data_deletion") as delete:
            result = handler.process_payload(db, {AUTH_CHANGE: [event]}, "t4")
        db.refresh(connection)
        assert result["revoked"] == 1
        assert connection.status == ConnectionStatus.REVOKED
        delete.assert_called_once_with(user.id, "zepp", reason="deregistration")


class TestRoute:
    def test_a_text_plain_push_is_accepted_and_queued(self, client: TestClient) -> None:
        with (
            patch("app.services.providers.zepp.webhook_handler.celery_app") as celery,
            patch("app.services.providers.zepp.webhook_handler.store_raw_payload"),
        ):
            response = client.post(
                "/api/v1/providers/zepp/webhooks?type=sleep",
                content=zepp_body({"sleeps": [SLEEP]}),
                headers={"Content-Type": "text/plain;charset=UTF-8"},
            )
        assert response.status_code == 200
        args = celery.send_task.call_args.kwargs["args"]
        assert args[0] == "zepp"
        assert len(args[1]["sleeps"]) == 1
