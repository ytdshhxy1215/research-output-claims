"""HTTP transport and envelope validation for the fixture connector."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from math import isfinite
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from .models import SourceScope

MAX_BODY_BYTES = 2 * 1024 * 1024


class IngestionError(Exception):
    def __init__(self, code, detail, *, retryable=False, retry_after=None, payload=None):
        super().__init__(detail)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.payload = payload


def utc_timestamp(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("expected a UTC timestamp")
    result = datetime.fromisoformat(value)
    if result.utcoffset() != timedelta(0):
        raise ValueError("expected a UTC timestamp")
    return result.astimezone(UTC)


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip())


def _retry_after(header):
    if not header:
        return None
    try:
        seconds = int(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except OverflowError, TypeError, ValueError:
            return None
    # Bound scheduling arithmetic and avoid busy looping on Retry-After: 0.
    return max(1, min(seconds, 86_400))


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_json_values(value):
    # PostgreSQL JSONB cannot store NUL or unpaired UTF-16 surrogates.
    if isinstance(value, str):
        value.encode("utf-8")
        if "\x00" in value:
            raise ValueError("JSON contains NUL")
    elif isinstance(value, float) and not isfinite(value):
        raise ValueError("JSON contains a non-finite number")
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_json_values(key)
            _validate_json_values(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json_values(item)


@dataclass(frozen=True)
class Manifest:
    release_id: str
    upper_bounds: dict[str, datetime]


@dataclass(frozen=True)
class Page:
    events: list
    next_cursor: str


class FixtureConnector:
    def __init__(self, base_url, *, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path, params=None):
        url = self.base_url + path + (f"?{urlencode(params)}" if params else "")
        try:
            with urlopen(url, timeout=self.timeout) as response:
                body = response.read(MAX_BODY_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            headers = exc.headers
            exc.close()
            if status == 429:
                raise IngestionError(
                    "rate_limited",
                    "HTTP 429",
                    retryable=True,
                    retry_after=_retry_after(headers.get("Retry-After")),
                ) from exc
            if status == 408 or 500 <= status <= 599:
                raise IngestionError(
                    "http_transient",
                    f"HTTP {status}",
                    retryable=True,
                    retry_after=_retry_after(headers.get("Retry-After")),
                ) from exc
            code = "http_auth" if status in (401, 403) else "http_contract"
            raise IngestionError(code, f"HTTP {status}") from exc
        except (OSError, IncompleteRead) as exc:
            raise IngestionError("network", str(exc), retryable=True) from exc
        try:
            if len(body) > MAX_BODY_BYTES:
                raise ValueError("response exceeded the size limit")
            result = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(result, dict):
                raise ValueError("expected a JSON object")
            _validate_json_values(result)
            return result
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise IngestionError("http_contract", f"invalid response: {exc}") from exc

    def manifest(self):
        data = self._get("/v1/manifest")
        try:
            release_id = data["release_id"]
            if not _text(release_id, 128):
                raise ValueError("invalid release_id")
            bounds = {
                stream: utc_timestamp(data["streams"][stream]["upper_bound"])
                for stream in SourceScope.Stream.values
            }
        except (KeyError, ValueError, TypeError) as exc:
            raise IngestionError("manifest_contract", f"invalid manifest: {exc}") from exc
        return Manifest(release_id, bounds)

    def page(self, run):
        params = {
            "release_id": run.release_id,
            "from": run.window_start.astimezone(UTC).isoformat(),
            "to": run.window_end.astimezone(UTC).isoformat(),
            "cursor": run.cursor,
            "limit": run.page_limit,
        }
        data = self._get(f"/v1/{run.stream}", params)
        try:
            events = data["events"]
            cursor = data["next_cursor"]
            if not isinstance(events, list) or len(events) > run.page_limit:
                raise ValueError("invalid events list")
            if cursor is not None and (
                not _text(cursor, 1024) or not events or cursor == run.cursor
            ):
                raise ValueError("non-progressing cursor")
        except (KeyError, ValueError, TypeError) as exc:
            raise IngestionError("page_contract", f"invalid page: {exc}") from exc
        return Page(events, cursor or "")
