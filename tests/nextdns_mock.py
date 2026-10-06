"""An offline stand-in for the NextDNS logs API, served over real HTTP."""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sverdrup.db import parse_timestamp

API_KEY = "test-api-key-not-a-real-secret"
PROFILE_ID = "abc123"


def log_entry(
    timestamp: str,
    domain: str,
    device: str | None = "Living Room TV",
    client_ip: str = "192.0.2.10",
) -> dict:
    """Build a log entry shaped like the NextDNS API's."""
    entry = {
        "timestamp": timestamp,
        "domain": domain,
        "root": ".".join(domain.split(".")[-2:]),
        "encrypted": True,
        "protocol": "DNS-over-HTTPS",
        "clientIp": client_ip,
        "status": "default",
        "reasons": [],
    }
    if device is not None:
        entry["device"] = {"id": device.lower().replace(" ", "-"), "name": device}
    return entry


class MockNextDNS:
    """Serve ``GET /profiles/:id/logs`` with from/sort/limit/cursor support.

    ``accounts`` maps each API key to the profile IDs its account owns, and
    ``logs`` maps profile IDs to their entries; ``entries`` is shorthand for
    the default PROFILE_ID's logs. ``failures`` is a queue of (status,
    headers) responses returned before any real page, to exercise rate
    limiting. ``requests`` records each request's profile, query parameters
    and API key header.
    """

    def __init__(self) -> None:
        self.accounts: dict[str, set[str]] = {API_KEY: {PROFILE_ID}}
        self.logs: dict[str, list[dict]] = {PROFILE_ID: []}
        self.failures: deque[tuple[int, dict[str, str]]] = deque()
        self.requests: list[dict] = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                mock._handle(self)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def entries(self) -> list[dict]:
        return self.logs[PROFILE_ID]

    @entries.setter
    def entries(self, value: list[dict]) -> None:
        self.logs[PROFILE_ID] = value

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> MockNextDNS:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        match = re.fullmatch(r"/profiles/([^/]+)/logs", parsed.path)
        profile = match.group(1) if match else None
        api_key = handler.headers.get("X-Api-Key")
        self.requests.append({"profile": profile, "params": params, "api_key": api_key})
        if api_key not in self.accounts:
            return self._send(handler, 403, {"errors": [{"code": "forbidden"}]})
        if profile not in self.accounts[api_key]:
            return self._send(handler, 404, {"errors": [{"code": "notFound"}]})
        if self.failures:
            status, headers = self.failures.popleft()
            return self._send(handler, status, {"errors": [{"code": "x"}]}, headers)

        def at(entry: dict) -> datetime:
            try:
                return parse_timestamp(entry["timestamp"])
            except (KeyError, TypeError, ValueError):
                return datetime.min.replace(
                    tzinfo=timezone.utc
                )  # malformed: sort first

        selected = list(self.logs.get(profile, []))
        if "from" in params:
            start = parse_timestamp(params["from"])
            selected = [e for e in selected if at(e) >= start]
        selected.sort(key=at, reverse=params.get("sort", "desc") == "desc")
        offset = int(params.get("cursor", "0"))
        limit = int(params.get("limit", "100"))
        page = selected[offset : offset + limit]
        more = offset + limit < len(selected)
        body = {
            "data": page,
            "meta": {"pagination": {"cursor": str(offset + limit) if more else None}},
        }
        self._send(handler, 200, body)

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(payload)


def utc(*args: int) -> datetime:
    """Shorthand for an aware UTC datetime."""
    return datetime(*args, tzinfo=timezone.utc)
