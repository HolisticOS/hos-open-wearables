"""Tests for the SDK import_service transient-vs-permanent error split (OW-01/02/03).

``import_data_from_request`` used to catch every exception and convert it into
a permanent 400 response, including infra-level failures (Redis lock
contention, a DB write during sleep finalize) that are likely to succeed on
retry. It must now re-raise ``TransientImportError`` (and its subclasses)
instead, so ``process_sdk_upload``'s ``autoretry_for`` policy can actually
retry the batch — see the 2026-09-12 pipeline audit, fixes OW-01/OW-02/OW-03.
"""

from logging import getLogger
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.services.apple.healthkit.exceptions import (
    SleepLockTimeoutError,
    TransientImportError,
)
from app.services.apple.healthkit.import_service import ImportService


class TestTransientImportErrorPropagates:
    @patch("app.services.apple.healthkit.import_service.ImportService.load_data")
    @patch("app.repositories.user_connection_repository.UserConnectionRepository.get_by_user_and_provider")
    def test_transient_error_from_load_data_propagates(
        self,
        mock_get_connection: MagicMock,
        mock_load_data: MagicMock,
        db: Session,
    ) -> None:
        """A TransientImportError (e.g. a lock timeout surfaced from sleep
        processing) must propagate out of import_data_from_request unchanged
        — not get logged-and-swallowed into a 400 response."""
        service = ImportService(log=getLogger(__name__))
        user_id = str(uuid4())
        mock_load_data.side_effect = SleepLockTimeoutError(f"Could not acquire lock for {user_id}")

        with pytest.raises(TransientImportError):
            service.import_data_from_request(
                db_session=db,
                request_content='{"data": {"provider": "apple", "records": [], "workouts": [], "sleep": []}}',
                content_type="application/json",
                user_id=user_id,
            )

        # A genuinely transient failure must never be reported to the caller
        # as a permanent "bad import" — that's the connection-update step
        # this test proves we never reach.
        mock_get_connection.assert_not_called()

    @patch("app.services.apple.healthkit.import_service.ImportService.load_data")
    def test_non_transient_error_still_returns_400(
        self,
        mock_load_data: MagicMock,
        db: Session,
    ) -> None:
        """A genuine bad-payload error (anything that isn't a
        TransientImportError) must keep today's behavior: logged and reported
        as a permanent 400, not retried forever."""
        service = ImportService(log=getLogger(__name__))
        user_id = str(uuid4())
        mock_load_data.side_effect = ValueError("malformed workout duration")

        response = service.import_data_from_request(
            db_session=db,
            request_content='{"data": {"provider": "apple", "records": [], "workouts": [], "sleep": []}}',
            content_type="application/json",
            user_id=user_id,
        )

        assert response.status_code == 400
