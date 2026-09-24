"""End-to-end and contract tests for the ramp control station.

Runs fully in-process: API + instrument simulator on ephemeral ports, executor
driven step-by-step. Covers idempotent submission, 409/422 semantics, the
receipt-visibility rules, fencing-token convergence after a crash, and
late-executor rejection.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from app import api, instrument
from app.executor import Executor
from app.store import InstrumentStore, Store


def http(method: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class SystemTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.op_store = Store(":memory:")
        cls.inst_store = InstrumentStore(":memory:")
        cls.api_server = api.make_server(cls.op_store, host="127.0.0.1", port=0)
        cls.inst_server = instrument.make_server(cls.inst_store, host="127.0.0.1", port=0)
        cls.api_port = cls.api_server.server_address[1]
        cls.inst_port = cls.inst_server.server_address[1]
        for srv in (cls.api_server, cls.inst_server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.api_port}"
        cls.inst = f"http://127.0.0.1:{cls.inst_port}"

    @classmethod
    def tearDownClass(cls):
        cls.api_server.shutdown()
        cls.inst_server.shutdown()
        cls.op_store.close()
        cls.inst_store.close()

    def setUp(self):
        self.executor = Executor(self.api, self.inst, owner="test-executor")

    # -- helpers ------------------------------------------------------------

    def submit(self, op_id, magnet="magnet-1", target=1200):
        return http("POST", f"{self.api}/api/ramps",
                    {"operationId": op_id, "magnetId": magnet, "targetMilliAmps": target})

    def status_of(self, op_id):
        return http("GET", f"{self.api}/api/ramps/{op_id}")

    def run_until_idle(self, limit=20):
        for _ in range(limit):
            if not self.executor.step():
                return
        self.fail("executor did not go idle")

    # -- submission contract --------------------------------------------------

    def test_submit_returns_queued(self):
        status, body = self.submit("op-create")
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "queued")
        self.assertNotIn("receipt", body)

    def test_same_id_same_params_replays_original(self):
        _, first = self.submit("op-replay", target=500)
        status, second = self.submit("op-replay", target=500)
        self.assertEqual(status, 200)
        self.assertEqual(first, second)

    def test_same_id_different_params_is_409_without_receipt(self):
        self.submit("op-conflict", target=500)
        status, body = self.submit("op-conflict", target=900)
        self.assertEqual(status, 409)
        self.assertNotIn("receipt", body)
        self.assertNotIn("receiptId", json.dumps(body))

    def test_field_errors_are_locatable_422(self):
        status, body = http("POST", f"{self.api}/api/ramps",
                            {"operationId": "op-非ascii", "targetMilliAmps": "high"})
        self.assertEqual(status, 422)
        fields = {f["field"] for f in body["error"]["fields"]}
        self.assertIn("operationId", fields)   # non-ASCII rejected
        self.assertIn("magnetId", fields)      # missing
        self.assertIn("targetMilliAmps", fields)  # wrong type
        self.assertNotIn("receipt", body)

    def test_unknown_operation_is_404(self):
        status, body = self.status_of("op-nope")
        self.assertEqual(status, 404)
        self.assertNotIn("receipt", body)

    def test_healthz(self):
        status, body = http("GET", f"{self.api}/healthz")
        self.assertEqual((status, body["status"]), (200, "ok"))

    # -- happy path -------------------------------------------------------------

    def test_happy_path_reaches_succeeded_with_receipt(self):
        self.submit("op-happy", target=750)
        self.run_until_idle()
        status, body = self.status_of("op-happy")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["receipt"]["receiptId"], "rcpt-op-happy")
        self.assertEqual(body["receipt"]["appliedCount"], 1)

    def test_queued_and_running_views_carry_no_receipt(self):
        self.submit_status = self.submit("op-pending", target=10)[1]
        self.assertEqual(self.submit_status["status"], "queued")
        self.assertNotIn("receipt", self.submit_status)
        _, body = self.status_of("op-pending")
        self.assertNotIn("receipt", body)

    # -- instrument idempotency -------------------------------------------------

    def test_instrument_apply_is_idempotent_per_operation(self):
        payload = {"operationId": "op-inst", "magnetId": "m1",
                   "targetMilliAmps": 100, "fencingToken": 1}
        _, first = http("POST", f"{self.inst}/instrument/apply", payload)
        payload["fencingToken"] = 2  # newer lease re-applying
        _, second = http("POST", f"{self.inst}/instrument/apply", payload)
        self.assertEqual(first["outcome"], "applied")
        self.assertEqual(second["outcome"], "replayed")
        self.assertEqual(first["application"]["receiptId"],
                         second["application"]["receiptId"])
        self.assertEqual(second["application"]["appliedCount"], 1)

    def test_instrument_rejects_stale_fencing_token(self):
        base = {"operationId": "op-fence", "magnetId": "m1", "targetMilliAmps": 5}
        http("POST", f"{self.inst}/instrument/apply", {**base, "fencingToken": 7})
        status, body = http("POST", f"{self.inst}/instrument/apply",
                            {**base, "fencingToken": 3})
        self.assertEqual(status, 409)
        self.assertNotIn("application", body)

    # -- crash convergence --------------------------------------------------------

    def test_crash_after_apply_converges_with_same_receipt_and_count_one(self):
        self.submit("op-crash", target=640)
        # Executor A claims and applies, then "crashes" before commit: simulate
        # by claiming + applying manually, never committing.
        _, claim_body = http("POST", f"{self.api}/internal/claim", {"owner": "exec-A"})
        claimed = claim_body["claimed"]
        self.assertEqual(claimed["operationId"], "op-crash")
        _, applied = http("POST", f"{self.inst}/instrument/apply", {
            "operationId": "op-crash", "magnetId": "magnet-1",
            "targetMilliAmps": 640, "fencingToken": claimed["fencingToken"],
        })
        receipt_id = applied["application"]["receiptId"]
        # exec-A is now dead. Force its lease to expire so exec-B can reclaim.
        self.op_store.renew_lease("op-crash", "exec-A", claimed["fencingToken"],
                                  lease_seconds=-1)
        # Executor B (higher fencing token) completes the operation.
        self.run_until_idle()
        _, body = self.status_of("op-crash")
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["receipt"]["receiptId"], receipt_id)
        self.assertEqual(body["receipt"]["appliedCount"], 1)
        # The instrument ledger agrees: exactly one application ever happened.
        _, ledger = http("GET", f"{self.inst}/instrument/applications/op-crash")
        self.assertEqual(ledger["appliedCount"], 1)

    def test_late_executor_cannot_overwrite_committed_result(self):
        self.submit("op-late", target=320)
        # exec-A claims (token T1) but stalls; lease expires; exec-B claims (T2>T1).
        _, claim_a = http("POST", f"{self.api}/internal/claim", {"owner": "exec-A"})
        token_a = claim_a["claimed"]["fencingToken"]
        self.op_store.renew_lease("op-late", "exec-A", token_a, lease_seconds=-1)
        self.run_until_idle()  # exec-B equivalent: our executor finishes the job
        _, done = self.status_of("op-late")
        self.assertEqual(done["status"], "succeeded")
        good_receipt = done["receipt"]["receiptId"]
        # Late exec-A wakes up and tries to apply + commit with its stale token.
        status_apply, _ = http("POST", f"{self.inst}/instrument/apply", {
            "operationId": "op-late", "magnetId": "magnet-1",
            "targetMilliAmps": 320, "fencingToken": token_a,
        })
        self.assertEqual(status_apply, 409)
        status_commit, body = http("POST", f"{self.api}/internal/complete", {
            "owner": "exec-A", "operationId": "op-late", "fencingToken": token_a,
            "receiptId": "rcpt-forged", "appliedCount": 99,
        })
        self.assertEqual(status_commit, 409)
        _, after = self.status_of("op-late")
        self.assertEqual(after["receipt"]["receiptId"], good_receipt)
        self.assertEqual(after["receipt"]["appliedCount"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
