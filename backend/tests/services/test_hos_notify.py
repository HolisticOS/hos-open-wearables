"""Tests for the HOS data-arrived notify: first data goes at once, a burst
inside the window folds into one follow-up, and nothing runs when unset."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

import app.integrations.celery.tasks.notify_hos_task as notify_task
import app.integrations.celery.tasks.webhook_push_task as push_task
from app.config import settings
from app.services import hos_notify


@pytest.fixture
def enabled() -> Generator[None, None, None]:
    with (
        patch.object(settings, "hos_notify_url", "https://hos.example/api/v1/webhooks/open-wearables"),
        patch.object(settings, "hos_notify_service_key", SecretStr("svc-key")),
        patch.object(settings, "hos_notify_window_seconds", 10),
    ):
        yield


@pytest.fixture
def celery() -> Generator[MagicMock, None, None]:
    with patch("celery.current_app") as app:
        yield app


def _sent(celery: MagicMock) -> list[tuple[list[Any], int | None]]:
    return [(c.kwargs["args"], c.kwargs.get("countdown")) for c in celery.send_task.call_args_list]


class TestSchedule:
    def test_off_when_unconfigured(self, celery: MagicMock) -> None:
        with patch.object(settings, "hos_notify_url", None):
            hos_notify.schedule_hos_notify([uuid4()], "garmin")
        celery.send_task.assert_not_called()

    def test_first_data_sends_now_and_queues_follow_up(self, enabled: None, celery: MagicMock) -> None:
        uid = str(uuid4())
        hos_notify.schedule_hos_notify([uid], "garmin")
        assert _sent(celery) == [([uid, "garmin", False], None), ([uid, "garmin", True], 10)]

    def test_burst_inside_window_only_marks_pending(self, enabled: None, celery: MagicMock) -> None:
        uid = str(uuid4())
        for _ in range(5):
            hos_notify.schedule_hos_notify([uid], "garmin")
        assert len(celery.send_task.call_args_list) == 2
        assert hos_notify.take_pending(uid) is True
        assert hos_notify.take_pending(uid) is False

    def test_users_are_independent(self, enabled: None, celery: MagicMock) -> None:
        a, b = str(uuid4()), str(uuid4())
        hos_notify.schedule_hos_notify([a, b, a], "garmin")
        assert {args[0] for args, _ in _sent(celery)} == {a, b}
        assert len(celery.send_task.call_args_list) == 4

    def test_redis_failure_never_raises(self, enabled: None, celery: MagicMock) -> None:
        with patch.object(hos_notify, "get_redis_client", side_effect=RuntimeError("down")):
            hos_notify.schedule_hos_notify([uuid4()], "garmin")
        celery.send_task.assert_not_called()


class TestNotifyTask:
    def test_posts_ow_user_id_with_service_key(self, enabled: None) -> None:
        uid = str(uuid4())
        with patch.object(notify_task.httpx, "post") as post:
            post.return_value = httpx.Response(200, request=httpx.Request("POST", settings.hos_notify_url or ""))
            result = notify_task.notify_hos.run(uid, "garmin")
        assert result["status"] == "sent"
        post.assert_called_once()
        assert post.call_args.kwargs["json"] == {"ow_user_ids": [uid], "provider": "garmin"}
        assert post.call_args.kwargs["headers"] == {"X-Service-Key": "svc-key"}

    def test_follow_up_skips_when_nothing_arrived(self, enabled: None) -> None:
        with patch.object(notify_task.httpx, "post") as post:
            result = notify_task.notify_hos.run(str(uuid4()), "garmin", True)
        assert result["status"] == "nothing_pending"
        post.assert_not_called()

    def test_follow_up_sends_when_data_arrived(self, enabled: None, celery: MagicMock) -> None:
        uid = str(uuid4())
        hos_notify.schedule_hos_notify([uid], "garmin")
        hos_notify.schedule_hos_notify([uid], "garmin")  # inside the window
        with patch.object(notify_task.httpx, "post") as post:
            post.return_value = httpx.Response(200, request=httpx.Request("POST", settings.hos_notify_url or ""))
            result = notify_task.notify_hos.run(uid, "garmin", True)
        assert result["status"] == "sent"


class TestSavedUserIds:
    def test_garmin_lists_every_synced_user(self) -> None:
        ids = [str(uuid4()), str(uuid4())]
        assert push_task._saved_user_ids({"synced_user_ids": ids, "processed": 2}) == ids

    def test_garmin_with_nothing_synced(self) -> None:
        assert push_task._saved_user_ids({"synced_user_ids": [], "processed": 0}) == []

    def test_single_user_provider_saved(self) -> None:
        uid = str(uuid4())
        assert push_task._saved_user_ids({"status": "saved", "user_id": uid, "records_saved": 3}) == [uid]

    @pytest.mark.parametrize(
        "result",
        [
            {"status": "duplicate", "user_id": "u", "records_saved": 3},
            {"status": "saved", "user_id": "u", "records_saved": 0},
            {"status": "error", "user_id": "u"},
            None,
        ],
    )
    def test_nothing_saved_means_no_notify(self, result: Any) -> None:
        assert push_task._saved_user_ids(result) == []
