"""Connect check page: signed tester links, the page, its data feed, connect and disconnect."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.connect_check_service import make_token, read_token
from app.utils.exceptions import NotFoundError
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers

BASE = "/api/v1/connect-check"
LATER = datetime.now(timezone.utc) + timedelta(days=1)


class TestToken:
    def test_round_trip(self) -> None:
        user_id = uuid4()
        assert read_token(make_token(user_id, "zepp", LATER)) == (user_id, "zepp")

    @pytest.mark.parametrize(
        "mangle", [lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"), lambda t: "x" + t, str.upper]
    )
    def test_a_tampered_link_is_not_found(self, mangle) -> None:  # noqa: ANN001
        with pytest.raises(NotFoundError):
            read_token(mangle(make_token(uuid4(), "zepp", LATER)))

    def test_an_expired_link_is_not_found(self) -> None:
        token = make_token(uuid4(), "zepp", datetime.now(timezone.utc) - timedelta(seconds=1))
        with pytest.raises(NotFoundError):
            read_token(token)

    def test_garbage_is_not_found(self) -> None:
        with pytest.raises(NotFoundError):
            read_token("not-a-token")


class TestRoutes:
    def test_minting_a_link_needs_an_api_key(self, client: TestClient) -> None:
        assert client.post(f"{BASE}/links", json={"provider": "zepp"}).status_code == 401

    def test_mint_open_and_read(self, client: TestClient, db: Session) -> None:
        api_key = ApiKeyFactory()
        minted = client.post(
            f"{BASE}/links", json={"provider": "zepp", "label": "Tester"}, headers=api_key_headers(api_key.plain_key)
        )
        assert minted.status_code == 201
        path = minted.json()["url"].split("/api/v1", 1)[1]

        page = client.get(f"/api/v1{path}")
        assert page.status_code == 200
        assert "Connect check" in page.text
        assert page.headers["cache-control"] == "no-store"

        data = client.get(f"/api/v1{path}/data").json()
        assert data["provider"] == "zepp"
        assert data["connection"]["connected"] is False
        assert data["sleep"] == []
        assert data["workouts"] == []

    def test_an_unknown_provider_cannot_be_minted(self, client: TestClient) -> None:
        api_key = ApiKeyFactory()
        response = client.post(f"{BASE}/links", json={"provider": "nope"}, headers=api_key_headers(api_key.plain_key))
        assert response.status_code == 400

    def test_a_bad_link_is_a_404(self, client: TestClient) -> None:
        assert client.get(f"{BASE}/nope").status_code == 404
        assert client.get(f"{BASE}/nope/data").status_code == 404

    def test_connect_redirects_to_the_provider_and_back_to_the_page(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        token = make_token(user.id, "zepp", LATER)
        with patch("app.services.providers.templates.base_oauth.get_redis_client") as redis:
            response = client.get(f"{BASE}/{token}/connect", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("https://user.zepp.com/oauth2/index.html#/login?")
        stored = redis.return_value.setex.call_args.args[2]
        assert f"/connect-check/{token}" in stored  # the OAuth callback sends the tester back here

    def test_a_connected_user_shows_connected(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="zepp", provider_user_id="300123")
        data = client.get(f"{BASE}/{make_token(user.id, 'zepp', LATER)}/data").json()
        assert data["connection"]["connected"] is True

    def test_disconnect(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="zepp", provider_user_id="300123")
        token = make_token(user.id, "zepp", LATER)
        with (
            patch("app.services.user_connection_service.schedule_provider_data_deletion"),
            patch("app.services.providers.zepp.oauth.ZeppOAuth.deregister_user") as deregister,
        ):
            assert client.post(f"{BASE}/{token}/disconnect").json() == {"disconnected": True}
        deregister.assert_called_once()
        assert client.get(f"{BASE}/{token}/data").json()["connection"]["connected"] is False
