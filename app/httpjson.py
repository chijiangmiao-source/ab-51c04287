"""Tiny JSON-over-HTTP helpers on top of the standard library.

Kept deliberately small: request parsing, field validation with locatable
errors (422), and uniform error envelopes that never leak success payloads.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or []

    def to_body(self) -> dict:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.fields:
            body["error"]["fields"] = self.fields
        return body


def unprocessable(fields) -> "ApiError":
    return ApiError(422, "validation_failed", "request validation failed", fields)


def conflict(message: str) -> "ApiError":
    return ApiError(409, "conflict", message)


def not_found(message: str) -> "ApiError":
    return ApiError(404, "not_found", message)


def read_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise unprocessable([{"field": "body", "issue": "must be a JSON object"}])


# -- field validators: each returns (value, error-or-None) --------------------

def req_ascii_string(data: dict, field: str, max_len: int = 128):
    value = data.get(field)
    if not isinstance(value, str) or not value:
        return None, {"field": field, "issue": "required non-empty string"}
    if not value.isascii():
        return None, {"field": field, "issue": "must contain ASCII characters only"}
    if len(value) > max_len:
        return None, {"field": field, "issue": f"must be at most {max_len} characters"}
    return value, None


def req_int(data: dict, field: str):
    value = data.get(field)
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        return None, {"field": field, "issue": "required integer"}
    return value, None


def make_handler(routes: dict[tuple[str, str], Callable]):
    """Build a BaseHTTPRequestHandler subclass dispatching (method, path)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "magnet-ramp/1.0"

        def log_message(self, fmt, *args):  # keep logs quiet; verify does its own reporting
            pass

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str):
            path = self.path.split("?", 1)[0]
            handler = routes.get((method, path))
            if handler is None:
                # Distinguish 404 (unknown path) from 405 (known path, wrong verb).
                if any(p == path for (m, p) in routes if m != method):
                    err = ApiError(405, "method_not_allowed", f"{method} not allowed on {path}")
                else:
                    err = not_found(f"no route for {method} {path}")
                self._send(err.status, err.to_body())
                return
            try:
                status, payload = handler(self)
                self._send(status, payload)
            except ApiError as err:
                self._send(err.status, err.to_body())
            except BrokenPipeError:
                pass
            except Exception as err:  # pragma: no cover - defensive
                self._send(500, {"error": {"code": "internal", "message": str(err)}})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(host: str, port: int, routes: dict[tuple[str, str], Callable]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(routes))
    server.daemon_threads = True
    return server
