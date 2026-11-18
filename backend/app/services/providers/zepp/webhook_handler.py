"""Zepp (Amazfit) webhook handler — POST /api/v1/providers/zepp/webhooks[?type=sleep|hrv|activity]

Zepp POSTs ``text/plain``: a JSON array of *strings*, each a serialized object
such as ``{"sleeps": [...]}``, ``{"hrv": [...]}``, ``{"activityFiles": [...]}``
or an authorization-change event ``{"userId", "eventType": "authorizationChange",
"changeType": "delete"}``. Any 2xx is a receipt; anything else is retried, and
duplicates happen. The ``type`` query parameter Zepp's console appends is only
a label: the body says what it carries.

Zepp signs nothing and documents no secret header, and the callback URLs were
registered without a path token, so a request cannot be authenticated. What
keeps it safe is that data is only ever stored for a Zepp user id that already
has an active OW connection (``process_wellness_items``), and a revoke only
acts on such a connection too.
"""

import json
from typing import Any
from uuid import UUID, uuid4

from celery import current_app as celery_app
from fastapi import Request

from app.constants.celery_tasks import CeleryQueue, CeleryTask
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.services.outgoing_webhooks.events import on_connection_revoked
from app.services.provider_data_deletion import schedule_provider_data_deletion
from app.services.providers.garmin.handlers.wellness import process_wellness_items
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.providers.zepp.data_247 import ZEPP_TO_GARMIN_TYPE, Zepp247Data
from app.services.raw_payload_storage import store_raw_payload
from app.utils.exceptions import InvalidInputError
from app.utils.structured_logging import get_logger

logger = get_logger(__name__)

AUTH_CHANGE = "authorizationChange"


def parse_zepp_body(body: bytes) -> dict[str, list[dict[str, Any]]]:
    """Merge Zepp's array of JSON strings into ``{push key: [items]}``.

    Also takes an array of objects or a single object, since nothing about the
    body is versioned. Authorization changes are collected under ``authorizationChange``.
    """
    try:
        outer = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidInputError("Invalid JSON body") from exc
    elements = outer if isinstance(outer, list) else [outer]
    merged: dict[str, list[dict[str, Any]]] = {}
    for element in elements:
        if isinstance(element, str):
            try:
                element = json.loads(element)
            except json.JSONDecodeError:
                logger.warning("Zepp push element is not JSON", provider="zepp")
                continue
        if not isinstance(element, dict):
            continue
        if element.get("eventType") == AUTH_CHANGE:
            merged.setdefault(AUTH_CHANGE, []).append(element)
            continue
        for key, items in element.items():
            if isinstance(items, list):
                merged.setdefault(key, []).extend(i for i in items if isinstance(i, dict))
    return merged


class ZeppWebhookHandler(BaseWebhookHandler):
    def __init__(self, data_247: Zepp247Data) -> None:
        super().__init__("zepp")
        self.data_247 = data_247
        self.connection_repo = UserConnectionRepository()

    def extract_user_id(self, payload: dict[str, Any]) -> str | None:
        user_ids = {
            str(item["userId"])
            for items in payload.values()
            if isinstance(items, list)
            for item in items
            if isinstance(item, dict) and item.get("userId")
        }
        return ",".join(sorted(user_ids)) or None

    def verify_signature(self, request: Request, body: bytes) -> bool:
        # Nothing to verify: see the module docstring for what protects this endpoint.
        return True

    def parse_payload(self, body: bytes) -> dict[str, list[dict[str, Any]]]:
        return parse_zepp_body(body)

    def supported_event_types(self) -> list[str]:
        return [*ZEPP_TO_GARMIN_TYPE, AUTH_CHANGE]

    def dispatch(self, db: DbSession, payload: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """Hand the push to the worker and answer at once (Zepp wants a reply within 30 s)."""
        trace_id = str(uuid4())[:8]
        logger.info(
            "Received Zepp webhook",
            provider="zepp",
            trace_id=trace_id,
            item_counts={k: len(v) for k, v in payload.items()},
        )
        store_raw_payload(source="webhook", provider="zepp", payload=payload, trace_id=trace_id)
        celery_app.send_task(
            CeleryTask.PROCESS_WEBHOOK_PUSH, args=["zepp", payload, trace_id], queue=CeleryQueue.WEBHOOK_SYNC
        )
        return {"status": "accepted"}

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        errors: list[str] = []
        synced_user_ids: set[UUID] = set()
        results: dict[str, Any] = {}

        for zepp_key, garmin_type in ZEPP_TO_GARMIN_TYPE.items():
            items = payload.get(zepp_key)
            if not items:
                continue
            result = process_wellness_items(
                db, self.connection_repo, self.data_247, garmin_type, items, errors, synced_user_ids, trace_id
            )
            results[zepp_key] = {"processed": result["processed"], "saved": result["saved"]}

        db.commit()
        for uid in synced_user_ids:
            if connection := self.connection_repo.get_active_connection(db, uid, "zepp"):
                self.connection_repo.update_last_synced_at(db, connection)

        revoked = self._process_auth_changes(db, payload.get(AUTH_CHANGE) or [], trace_id)
        return {
            "results": results,
            "revoked": revoked,
            "errors": errors,
            "synced_user_ids": sorted(str(uid) for uid in synced_user_ids),
        }

    def _process_auth_changes(self, db: DbSession, events: list[dict[str, Any]], trace_id: str) -> int:
        """The member revoked us in the Zepp app: end the connection and delete its Zepp data."""
        revoked = 0
        for event in events:
            zepp_user_id = event.get("userId")
            if not zepp_user_id or event.get("changeType", "delete") != "delete":
                continue
            for connection in self.connection_repo.get_all_by_provider_user_id(db, "zepp", str(zepp_user_id)):
                if connection.status == ConnectionStatus.REVOKED:
                    continue
                self.connection_repo.mark_as_revoked(db, connection)
                on_connection_revoked(
                    user_id=connection.user_id,
                    provider="zepp",
                    connection_id=connection.id,
                    reason="deregistration",
                    revoked_at=connection.updated_at.isoformat(),
                )
                schedule_provider_data_deletion(connection.user_id, "zepp", reason="deregistration")
                revoked += 1
                logger.info(
                    "Revoked Zepp connection on authorization change",
                    provider="zepp",
                    action="connection_revoked",
                    trace_id=trace_id,
                    user_id=str(connection.user_id),
                )
        return revoked
