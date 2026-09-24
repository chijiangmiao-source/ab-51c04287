"""Central configuration. Every knob is overridable via environment variables
so the same code runs locally, under docker compose, and inside verify."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw in (None, "") else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw in (None, "") else float(raw)


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --- API service -------------------------------------------------------------
API_HOST = _str("API_HOST", "0.0.0.0")
API_PORT = _int("API_PORT", 8080)
API_DB = _str("API_DB", "/tmp/magnet-api.db")

# --- Instrument simulator ----------------------------------------------------
INSTRUMENT_HOST = _str("INSTRUMENT_HOST", "0.0.0.0")
INSTRUMENT_PORT = _int("INSTRUMENT_PORT", 9100)
INSTRUMENT_DB = _str("INSTRUMENT_DB", "/tmp/magnet-instrument.db")

# Client-facing URL of the instrument simulator. Used by the executor to apply
# ramps and by the API to cross-check receipts before committing them.
INSTRUMENT_URL = _str("INSTRUMENT_URL", f"http://127.0.0.1:{INSTRUMENT_PORT}")

# --- Executor ----------------------------------------------------------------
EXECUTOR_API_URL = _str("EXECUTOR_API_URL", f"http://127.0.0.1:{API_PORT}")
EXECUTOR_ID = _str("EXECUTOR_ID", f"executor-{os.getpid()}")
CLAIM_WAIT_SECONDS = _float("CLAIM_WAIT_SECONDS", 2.0)
RENEW_INTERVAL_SECONDS = _float("RENEW_INTERVAL_SECONDS", 1.0)

# Lease time-to-live (seconds). The API stamps claims with this deadline; the
# executor heartbeats well inside it. After expiry another worker may reclaim
# the operation with a higher fencing token.
LEASE_SECONDS = _float("LEASE_SECONDS", 5.0)

# Fault injection: comma-separated operationIds for which the executor dies
# (os._exit) right after the instrument applied but before committing.
CRASH_AFTER_APPLY = _str("CRASH_AFTER_APPLY", "")
