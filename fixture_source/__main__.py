import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from .data import EVENTS, RELEASES, STREAMS


def utc_timestamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() != timedelta(0):
        raise ValueError("expected a UTC timestamp")
    return result


def cursor_for(stream, release_id, start, end, event):
    material = [stream, release_id, start.isoformat(), end.isoformat(), event["event_id"]]
    return hashlib.sha256(json.dumps(material).encode("utf-8")).hexdigest()


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, address, release_id="batch-1"):
        if release_id not in RELEASES:
            raise ValueError("unknown release_id")
        self.release_id = release_id
        self.failures = {}
        self.state_lock = Lock()
        super().__init__(address, FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    def json_response(self, status, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        target = urlsplit(self.path)
        if target.path == "/healthz":
            self.json_response(200, {"status": "ok", "source": "fixture"})
        elif target.path == "/v1/manifest":
            with self.server.state_lock:
                release_id = self.server.release_id
            self.json_response(
                200,
                {
                    "source": "fixture",
                    "release_id": release_id,
                    "streams": {
                        stream: {"upper_bound": RELEASES[release_id]} for stream in STREAMS
                    },
                },
            )
        elif target.path in ("/v1/works", "/v1/patents"):
            self.get_events(target.path.rsplit("/", 1)[-1], target.query)
        else:
            self.json_response(404, {"error": "unknown endpoint"})

    def get_events(self, stream, query_string):
        try:
            query = parse_qs(query_string, keep_blank_values=True, strict_parsing=True)
            if set(query) - {"release_id", "from", "to", "cursor", "limit"} or any(
                len(values) != 1 for values in query.values()
            ):
                raise ValueError("unknown or repeated parameters")
            release_id = query["release_id"][0]
            start = utc_timestamp(query["from"][0])
            end = utc_timestamp(query["to"][0])
            published = utc_timestamp(RELEASES[release_id])
            cursor = query.get("cursor", [""])[0]
            limit = int(query.get("limit", ["100"])[0])
            with self.server.state_lock:
                visible = utc_timestamp(RELEASES[self.server.release_id])
            if not 1 <= limit <= 100 or not start < end <= published <= visible:
                raise ValueError("invalid window, limit or unpublished release")
            events = sorted(
                (e for e in EVENTS[stream] if start <= utc_timestamp(e["updated_at"]) < end),
                key=lambda e: (utc_timestamp(e["updated_at"]), e["event_id"]),
            )
            cursors = [cursor_for(stream, release_id, start, end, e) for e in events]
            offset = cursors.index(cursor) + 1 if cursor else 0
        except (KeyError, ValueError) as exc:
            self.json_response(400, {"error": f"invalid page request: {exc}"})
            return

        with self.server.state_lock:
            fault = self.server.failures.get((stream, cursor))
            if fault:
                status, remaining, retry_after = fault
                if remaining > 1:
                    self.server.failures[stream, cursor] = (status, remaining - 1, retry_after)
                else:
                    del self.server.failures[stream, cursor]
        if fault:
            headers = {"Retry-After": retry_after} if retry_after is not None else None
            self.json_response(status, {"error": "injected fixture failure"}, headers)
            return

        stop = offset + limit
        self.json_response(
            200,
            {
                "events": events[offset:stop],
                "next_cursor": cursors[stop - 1] if stop < len(events) else None,
            },
        )

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/__fixture/release", "/__fixture/failure"):
            self.json_response(404, {"error": "unknown endpoint"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("invalid Content-Length")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            if path == "/__fixture/release":
                release_id = data["release_id"]
                if not isinstance(release_id, str) or release_id not in RELEASES:
                    raise ValueError("unknown release_id")
                with self.server.state_lock:
                    if RELEASES[release_id] < RELEASES[self.server.release_id]:
                        raise ValueError("releases cannot move backwards")
                    self.server.release_id = release_id
            else:
                stream = data["stream"]
                cursor = data.get("cursor", "")
                status = data.get("status", 503)
                count = data.get("count", 1)
                retry_after = data.get("retry_after")
                if (
                    stream not in STREAMS
                    or not isinstance(cursor, str)
                    or type(status) is not int
                    or status not in (401, 403, 429, 500, 502, 503, 504)
                    or type(count) is not int
                    or not 0 <= count <= 1000
                    or (
                        retry_after is not None
                        and (type(retry_after) is not int or retry_after < 0)
                    )
                ):
                    raise ValueError("invalid fault parameters")
                with self.server.state_lock:
                    if count:
                        self.server.failures[stream, cursor] = status, count, retry_after
                    else:
                        self.server.failures.pop((stream, cursor), None)
        except (KeyError, ValueError, UnicodeError) as exc:
            self.json_response(400, {"error": str(exc)})
            return
        self.json_response(200, {"ok": True})


def main():
    parser = argparse.ArgumentParser(description="Synthetic research-output fixture API")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--release-id", choices=RELEASES, default=os.environ.get("FIXTURE_RELEASE_ID", "batch-1")
    )
    args = parser.parse_args()
    with FixtureServer((args.host, args.port), args.release_id) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
