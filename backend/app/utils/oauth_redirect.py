"""Helpers for the post-OAuth redirect: which URLs are allowed, and how query params are added."""

import re
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import settings

# Reason codes are echoed back to the client in a query string: keep them short and plain.
_REASON_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_REASON_LENGTH = 64


def allowed_redirect_prefixes() -> list[str]:
    """Prefixes a redirect_uri may start with: API_BASE_URL, FRONTEND_URL and the configured extras."""
    prefixes = [settings.api_base_url, settings.frontend_url, *settings.oauth_allowed_redirect_prefixes]
    return [p for p in prefixes if p]


def _split(url: str) -> SplitResult | None:
    if not url or any(ch.isspace() or ord(ch) < 0x20 for ch in url) or "\\" in url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return parts


def _matches_prefix(candidate: SplitResult, prefix: SplitResult) -> bool:
    """Same scheme and host (no prefix tricks like ``example.com.evil``), path under the prefix path."""
    if candidate.scheme.lower() != prefix.scheme.lower():
        return False
    if candidate.netloc.lower() != prefix.netloc.lower():
        return False
    prefix_path = prefix.path.rstrip("/")
    if not prefix_path:
        return True
    return candidate.path == prefix_path or candidate.path.startswith(prefix_path + "/")


def is_allowed_redirect_uri(redirect_uri: str) -> bool:
    """True if redirect_uri sits under one of the allowed prefixes."""
    candidate = _split(redirect_uri)
    if candidate is None:
        return False
    for raw_prefix in allowed_redirect_prefixes():
        prefix = _split(raw_prefix)
        if prefix is not None and _matches_prefix(candidate, prefix):
            return True
    return False


def add_query_params(url: str, params: dict[str, str], *, override: bool) -> str:
    """Return url with params added to its query string, keeping the params already there.

    With override=False a param that is already present is left as it is; with
    override=True its value is replaced.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    present = {key for key, _ in query}
    if override:
        query = [(key, value) for key, value in query if key not in params]
        query.extend(params.items())
    else:
        query.extend((key, value) for key, value in params.items() if key not in present)
    return urlunsplit(parts._replace(query=urlencode(query)))


def short_reason(raw: str | None, default: str = "connection_failed") -> str:
    """A short, URL-safe reason code."""
    cleaned = _REASON_UNSAFE.sub("_", raw or "").strip("_")[:_MAX_REASON_LENGTH]
    return cleaned or default
