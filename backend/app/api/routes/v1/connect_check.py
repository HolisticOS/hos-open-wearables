"""Connect check page — see app/services/connect_check_service.py.

POST /connect-check/links                (API key)  mint a tester link: {provider, label}
GET  /connect-check/{token}              (link)     the page
GET  /connect-check/{token}/connect      (link)     start the provider OAuth, back to the page
GET  /connect-check/{token}/data         (link)     connection + last 30 days, polled by the page
POST /connect-check/{token}/disconnect   (link)     disconnect and delete this provider's data
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.database import DbSession
from app.services import ApiKeyDep
from app.services.connect_check_service import connect_check_service, read_token

router = APIRouter()

PAGE = Path(__file__).resolve().parents[3] / "static" / "connect-check" / "index.html"
# The link is a credential: no caching, no referrer, no indexing.
PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


class ConnectCheckLinkCreate(BaseModel):
    provider: str = Field("zepp", description="Provider slug, e.g. zepp or garmin")
    label: str | None = Field(None, max_length=100, description="Who the link is for (stored as the user's name)")


@router.post("/connect-check/links", status_code=status.HTTP_201_CREATED)
def create_connect_check_link(body: ConnectCheckLinkCreate, db: DbSession, _api_key: ApiKeyDep) -> dict[str, Any]:
    """Create an OW user for a tester and a 14-day link to the connect check page."""
    return connect_check_service.create_link(db, body.provider, body.label)


@router.get("/connect-check/{token}", include_in_schema=False)
def connect_check_page(token: str) -> FileResponse:
    read_token(token)
    return FileResponse(PAGE, media_type="text/html", headers=PRIVATE_HEADERS)


@router.get("/connect-check/{token}/connect", include_in_schema=False)
def connect_check_connect(token: str) -> RedirectResponse:
    user_id, provider = read_token(token)
    url = connect_check_service.authorization_url(user_id, provider, token)
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER, headers=PRIVATE_HEADERS)


@router.get("/connect-check/{token}/data", include_in_schema=False)
def connect_check_data(token: str, db: DbSession) -> dict[str, Any]:
    user_id, provider = read_token(token)
    return connect_check_service.snapshot(db, user_id, provider)


@router.post("/connect-check/{token}/disconnect", include_in_schema=False)
def connect_check_disconnect(token: str, db: DbSession) -> dict[str, bool]:
    user_id, provider = read_token(token)
    connect_check_service.disconnect(db, user_id, provider)
    return {"disconnected": True}
