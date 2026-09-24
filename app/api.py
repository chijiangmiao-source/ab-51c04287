"""Ramp command API.

Public surface:
  POST /api/ramps                 submit a ramp command (idempotent by operationId)
  GET  /api/ramps/{operationId}   poll status and, once succeeded, the receipt
  GET  /healthz                   liveness/readiness probe

Executor surface (internal):
  POST /internal/claim            claim next operation with a time-boxed lease
  POST /internal/renew            extend the lease while still holding it
  POST /internal/complete         commit the instrument receipt (fencing-guarded)

State machine is exactly: queued -> running -> succeeded. Failure responses
never carry a receipt; the receipt appears only after a successful commit.
"""
from __future__ import annotations

import re
from http.server import ThreadingHTTPServer

from . import config
from .httpjson import (ApiError, conflict, make_handler, not_found, read_json,
                       req_ascii_string, req_int, unprocessable)
from .store import Store

_OP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_operation_id(data: dict):
    op_id, err = req_ascii_string(data, "operationId")
    if err:
        return None, err
    if not _OP_ID_RE.match(op_id):
        return None, {
            "field": "operationId",
            "issue": "must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        }
    return op_id, None


def _parse_submit(data: dict):
    fields = []
    op_id, err = _validate_operation_id(data)
    if err:
        fields.append(err)
    magnet_id, err = req_ascii_string(data, "magnetId")
    if err:
        fields.append(err)
    target_ma, err = req_int(data, "targetMilliAmps")
    if err:
        fields.append(err)
    if fields:
        raise unprocessable(fields)
    return op_id, magnet_id, target_ma


def _public_view(row: dict) -> dict:
    """Response body. The receipt is attached only for succeeded operations,
    so a non-success response can never carry a success receipt."""
    body = {
        "operationId": row["operation_id"],
        "magnetId": row["magnet_id"],
        "targetMilliAmps": row["target_ma"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if row["status"] == "succeeded":
        body["receipt"] = {
            "receiptId": row["receipt_id"],
            "appliedCount": row["applied_count"],
        }
    return body


def _parse_lease_call(data):
    """Shared validation for /internal/renew and /internal/complete."""
    if not isinstance(data, dict):
        raise unprocessable([{"field": "body", "issue": "must be a JSON object"}])
    fields = []
    owner = data.get("owner")
    if not isinstance(owner, str) or not owner:
        fields.append({"field": "owner", "issue": "required non-empty string"})
    op_id = data.get("operationId")
    if not isinstance(op_id, str) or not op_id:
        fields.append({"field": "operationId", "issue": "required non-empty string"})
    token = data.get("fencingToken")
    if isinstance(token, bool) or not isinstance(token, int):
        fields.append({"field": "fencingToken", "issue": "required integer"})
    if fields:
        raise unprocessable(fields)
    return owner, op_id, token


def build_routes(store: Store):
    def healthz(_req):
        return 200, {"status": "ok"}

    def submit(req):
        data = read_json(req)
        if not isinstance(data, dict):
            raise unprocessable([{"field": "body", "issue": "must be a JSON object"}])
        op_id, magnet_id, target_ma = _parse_submit(data)

        row, created = store.create_operation(op_id, magnet_id, target_ma)
        if created:
            return 201, _public_view(row)
        # Idempotent replay: same operationId AND same parameters -> return the
        # original command as stored (including its current status/receipt).
        if row["magnet_id"] == magnet_id and row["target_ma"] == target_ma:
            return 200, _public_view(row)
        # Same identifier reused with different parameters -> 409, no receipt.
        raise conflict(
            f"operationId '{op_id}' already exists with different parameters"
        )

    def query(req):
        op_id = req.path.split("/api/ramps/", 1)[1].split("?", 1)[0]
        row = store.get_operation(op_id)
        if row is None:
            raise not_found(f"operation '{op_id}' not found")
        return 200, _public_view(row)

    def claim(req):
        data = read_json(req)
        owner = data.get("owner") if isinstance(data, dict) else None
        if not isinstance(owner, str) or not owner:
            raise unprocessable([{"field": "owner", "issue": "required non-empty string"}])
        row = store.claim_next(owner, config.LEASE_SECONDS)
        if row is None:
            return 200, {"claimed": None}
        return 200, {
            "claimed": {
                "operationId": row["operation_id"],
                "magnetId": row["magnet_id"],
                "targetMilliAmps": row["target_ma"],
                "fencingToken": row["fencing_token"],
                "leaseExpiresAt": row["lease_expires_at"],
            }
        }

    def renew(req):
        owner, op_id, token = _parse_lease_call(read_json(req))
        if not store.renew_lease(op_id, owner, token, config.LEASE_SECONDS):
            raise conflict("lease not held (lost, expired, or fencing token superseded)")
        return 200, {"renewed": True, "operationId": op_id}

    def complete(req):
        data = read_json(req)
        owner, op_id, token = _parse_lease_call(data)
        fields = []
        receipt_id = data.get("receiptId")
        if not isinstance(receipt_id, str) or not receipt_id:
            fields.append({"field": "receiptId", "issue": "required non-empty string"})
        applied = data.get("appliedCount")
        if isinstance(applied, bool) or not isinstance(applied, int) or applied < 0:
            fields.append({"field": "appliedCount", "issue": "required non-negative integer"})
        if fields:
            raise unprocessable(fields)
        if not store.complete_operation(op_id, owner, token, receipt_id, applied):
            raise conflict("lease not held (lost, expired, or fencing token superseded)")
        return 200, _public_view(store.get_operation(op_id))

    return {
        ("GET", "/healthz"): healthz,
        ("POST", "/api/ramps"): submit,
        ("POST", "/internal/claim"): claim,
        ("POST", "/internal/renew"): renew,
        ("POST", "/internal/complete"): complete,
        ("GET", "/api/ramps/*"): query,  # prefix-dispatched in make_server
    }


def make_server(store: Store, host: str | None = None, port: int | None = None):
    routes = build_routes(store)
    query = routes.pop(("GET", "/api/ramps/*"))
    base_handler = make_handler(routes)

    class Handler(base_handler):
        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            if method == "GET" and path.startswith("/api/ramps/"):
                try:
                    status, payload = query(self)
                    self._send(status, payload)
                except ApiError as err:
                    self._send(err.status, err.to_body())
                return
            super()._dispatch(method)

    server = ThreadingHTTPServer(
        (host or config.API_HOST, port if port is not None else config.API_PORT), Handler
    )
    server.daemon_threads = True
    return server


def main():
    store = Store(config.API_DB)
    server = make_server(store)
    host, port = server.server_address[:2]
    print(f"[api] listening on {host}:{port} db={config.API_DB}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        store.close()


if __name__ == "__main__":
    main()
