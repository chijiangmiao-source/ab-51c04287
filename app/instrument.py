"""Instrument simulator — the magnet's idempotent application endpoint.

  POST /instrument/apply                      apply a ramp (idempotent by operationId)
  GET  /instrument/applications/{operationId} inspect the recorded application
  GET  /healthz                               liveness probe

The simulator owns its own ledger (separate SQLite file): once an operationId
has been applied, the receipt_id and applied_count are final. Every call must
present a fencing token; a token below the highest token ever seen for that
operation is rejected as stale, so a late executor can never re-apply or
overwrite what a newer lease holder already did.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer

from . import config
from .httpjson import (ApiError, conflict, make_handler, not_found, read_json,
                       req_ascii_string, req_int, unprocessable)
from .store import InstrumentStore


def _public_view(row: dict) -> dict:
    return {
        "operationId": row["operation_id"],
        "magnetId": row["magnet_id"],
        "targetMilliAmps": row["target_ma"],
        "receiptId": row["receipt_id"],
        "appliedCount": row["applied_count"],
        "maxFencingToken": row["max_fencing_token"],
    }


def build_routes(store: InstrumentStore):
    def healthz(_req):
        return 200, {"status": "ok"}

    def apply(req):
        data = read_json(req)
        if not isinstance(data, dict):
            raise unprocessable([{"field": "body", "issue": "must be a JSON object"}])
        fields = []
        op_id, err = req_ascii_string(data, "operationId")
        if err:
            fields.append(err)
        magnet_id, err = req_ascii_string(data, "magnetId")
        if err:
            fields.append(err)
        target_ma, err = req_int(data, "targetMilliAmps")
        if err:
            fields.append(err)
        token, err = req_int(data, "fencingToken")
        if err:
            fields.append(err)
        if fields:
            raise unprocessable(fields)

        outcome, row = store.apply(op_id, magnet_id, target_ma, token)
        if outcome == "stale":
            raise conflict(
                f"stale fencing token {token}: operation '{op_id}' already saw "
                f"token {row['max_fencing_token']}"
            )
        # "applied" on first application, "replayed" on idempotent retry.
        return 200, {"outcome": outcome, "application": _public_view(row)}

    def query(req):
        op_id = req.path.split("/instrument/applications/", 1)[1].split("?", 1)[0]
        row = store.get(op_id)
        if row is None:
            raise not_found(f"no application for '{op_id}'")
        return 200, _public_view(row)

    return {
        ("GET", "/healthz"): healthz,
        ("POST", "/instrument/apply"): apply,
        ("GET", "/instrument/applications/*"): query,
    }


def make_server(store: InstrumentStore, host: str | None = None, port: int | None = None):
    routes = build_routes(store)
    query = routes.pop(("GET", "/instrument/applications/*"))
    base_handler = make_handler(routes)

    class Handler(base_handler):
        def _dispatch(self, method):
            path = self.path.split("?", 1)[0]
            if method == "GET" and path.startswith("/instrument/applications/"):
                try:
                    status, payload = query(self)
                    self._send(status, payload)
                except ApiError as err:
                    self._send(err.status, err.to_body())
                return
            super()._dispatch(method)

    server = ThreadingHTTPServer(
        (host or config.INSTRUMENT_HOST,
         port if port is not None else config.INSTRUMENT_PORT),
        Handler,
    )
    server.daemon_threads = True
    return server


def main():
    store = InstrumentStore(config.INSTRUMENT_DB)
    server = make_server(store)
    host, port = server.server_address[:2]
    print(f"[instrument] listening on {host}:{port} db={config.INSTRUMENT_DB}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        store.close()


if __name__ == "__main__":
    main()
