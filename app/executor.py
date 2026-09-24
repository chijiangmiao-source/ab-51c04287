"""Ramp executor.

Loop: claim an operation under a time-boxed lease -> apply it on the
instrument simulator with the claim's fencing token -> commit the receipt back
to the API. A background thread renews the lease while work is in flight.

Crash/convergence contract: if this process dies after the instrument applied
but before the commit, the lease expires, another executor claims the
operation with a HIGHER fencing token, the instrument replays the SAME
receipt (applied_count stays 1), and the commit lands. A late/duplicate
commit from the stale token is rejected by the API's fencing guard.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import config


def _post(url: str, payload: dict, timeout: float = 10.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"error": {"code": "http_error", "message": str(exc)}}


class Executor:
    def __init__(self, api_url: str, instrument_url: str, owner: str,
                 crash_after_apply: set[str] | None = None):
        self._api = api_url.rstrip("/")
        self._instrument = instrument_url.rstrip("/")
        self._owner = owner
        self._crash_after_apply = set(crash_after_apply or ())
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    # -- lease maintenance -------------------------------------------------

    def _renew_loop(self, op_id: str, token: int, done: threading.Event):
        while not done.wait(config.RENEW_INTERVAL_SECONDS):
            status, body = _post(
                f"{self._api}/internal/renew",
                {"owner": self._owner, "operationId": op_id, "fencingToken": token},
            )
            if status != 200:
                print(f"[executor] renew rejected for {op_id}: {body}", flush=True)
                return  # lease lost; the fencing guard will reject our commit

    # -- main loop ----------------------------------------------------------

    def run_forever(self):
        print(f"[executor] {self._owner} polling {self._api}", flush=True)
        while not self._stop.is_set():
            try:
                worked = self.step()
            except Exception as exc:  # transient network errors etc.
                print(f"[executor] step error: {exc}", flush=True)
                worked = False
            if not worked:
                self._stop.wait(config.CLAIM_WAIT_SECONDS)

    def step(self) -> bool:
        """One claim-apply-commit cycle. Returns True if work was done."""
        status, body = _post(f"{self._api}/internal/claim", {"owner": self._owner})
        if status != 200:
            print(f"[executor] claim failed: {body}", flush=True)
            return False
        claimed = body.get("claimed")
        if not claimed:
            return False

        op_id = claimed["operationId"]
        token = claimed["fencingToken"]
        print(f"[executor] claimed {op_id} token={token}", flush=True)

        renew_done = threading.Event()
        renewer = threading.Thread(
            target=self._renew_loop, args=(op_id, token, renew_done), daemon=True
        )
        renewer.start()
        try:
            self._apply_and_commit(claimed)
        finally:
            renew_done.set()
            renewer.join(timeout=2.0)
        return True

    def _apply_and_commit(self, claimed: dict):
        op_id = claimed["operationId"]
        token = claimed["fencingToken"]

        status, body = _post(
            f"{self._instrument}/instrument/apply",
            {
                "operationId": op_id,
                "magnetId": claimed["magnetId"],
                "targetMilliAmps": claimed["targetMilliAmps"],
                "fencingToken": token,
            },
        )
        if status == 409:
            # Stale token: a newer lease holder owns this operation. Back off.
            print(f"[executor] apply rejected (stale) for {op_id}: {body}", flush=True)
            return
        if status != 200:
            print(f"[executor] apply failed for {op_id}: {body}", flush=True)
            return
        application = body["application"]

        # Fault injection for the convergence drill: die AFTER the instrument
        # applied but BEFORE committing. os._exit skips cleanup (like a crash).
        if op_id in self._crash_after_apply and body.get("outcome") == "applied":
            print(f"[executor] CRASH AFTER APPLY injected for {op_id}", flush=True)
            os._exit(1)

        status, body = _post(
            f"{self._api}/internal/complete",
            {
                "owner": self._owner,
                "operationId": op_id,
                "fencingToken": token,
                "receiptId": application["receiptId"],
                "appliedCount": application["appliedCount"],
            },
        )
        if status == 200:
            print(
                f"[executor] committed {op_id} receipt={application['receiptId']} "
                f"appliedCount={application['appliedCount']}",
                flush=True,
            )
        else:
            print(f"[executor] commit rejected for {op_id}: {body}", flush=True)


def main():
    crash_ops = {s for s in config.CRASH_AFTER_APPLY.split(",") if s}
    executor = Executor(
        api_url=config.EXECUTOR_API_URL,
        instrument_url=config.INSTRUMENT_URL,
        owner=config.EXECUTOR_ID,
        crash_after_apply=crash_ops,
    )
    try:
        executor.run_forever()
    except KeyboardInterrupt:
        executor.stop()


if __name__ == "__main__":
    main()
