"""单元测试：校验、API 状态机/fencing、仪器幂等落账。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import api  # noqa: E402
import common  # noqa: E402
import instrument  # noqa: E402


class ValidationTests(unittest.TestCase):
    def test_valid(self):
        clean, errors = common.validate_ramp_payload(
            {"operationId": "op-1", "magnetId": "M:01",
             "targetMilliamps": -1200})
        self.assertEqual(errors, [])
        self.assertEqual(clean["targetMilliamps"], -1200)

    def test_locatable_errors(self):
        _, errors = common.validate_ramp_payload(
            {"operationId": "非ascii!", "magnetId": "",
             "targetMilliamps": 3.5})
        fields = {e["field"] for e in errors}
        self.assertEqual(
            fields, {"/operationId", "/magnetId", "/targetMilliamps"})

    def test_bool_is_not_int(self):
        _, errors = common.validate_ramp_payload(
            {"operationId": "op", "magnetId": "m",
             "targetMilliamps": True})
        self.assertTrue(any(e["field"] == "/targetMilliamps" for e in errors))

    def test_range(self):
        _, errors = common.validate_ramp_payload(
            {"operationId": "op", "magnetId": "m",
             "targetMilliamps": 10 ** 9})
        self.assertTrue(errors)


class ApiStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api.DB_PATH = os.path.join(self.tmp.name, "api.db")
        api.LEASE_TTL_SECONDS = 0.2
        api.init_db()
        self.payload = {"operationId": "op-1", "magnetId": "M1",
                        "targetMilliamps": 500}

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_create_and_conflict(self):
        with api.closing(api.get_conn()) as conn:
            status, body = api.create_ramp(conn, self.payload)
            self.assertEqual(status, 201)
            self.assertEqual(body["status"], "queued")
        with api.closing(api.get_conn()) as conn:
            status2, body2 = api.create_ramp(conn, self.payload)
            self.assertEqual(status2, 200)
            self.assertEqual(body2["operationId"], "op-1")
        with self.assertRaises(api.ApiError) as ctx:
            with api.closing(api.get_conn()) as conn:
                api.create_ramp(conn, {**self.payload, "targetMilliamps": 9})
        self.assertEqual(ctx.exception.status, 409)

    def test_fencing_tokens_increment_and_stale_rejected(self):
        with api.closing(api.get_conn()) as conn:
            api.create_ramp(conn, self.payload)

        with api.closing(api.get_conn()) as conn:
            lease1 = api.acquire_lease(conn, "exec-A")
        self.assertEqual(lease1["fenceToken"], 1)

        # 租约未过期：同任务不会被重复领取
        with api.closing(api.get_conn()) as conn:
            self.assertIsNone(api.acquire_lease(conn, "exec-A"))

        # 迟到的旧持有者不能提交（fence 不匹配）
        with api.closing(api.get_conn()) as conn:
            accepted, row = api.submit_result(
                conn, "op-1", "exec-STALE", 1, "rcpt-FORGE", 1)
        self.assertFalse(accepted)

        # 租约过期后续领，token 递增
        import time
        time.sleep(0.25)
        with api.closing(api.get_conn()) as conn:
            lease2 = api.acquire_lease(conn, "exec-B")
        self.assertEqual(lease2["fenceToken"], 2)

        # 第一任执行者迟到提交不能覆盖新持有者
        with api.closing(api.get_conn()) as conn:
            accepted, _ = api.submit_result(
                conn, "op-1", "exec-A", 1, "rcpt-OLD", 1)
        self.assertFalse(accepted)

        # 当前持有者提交成功
        with api.closing(api.get_conn()) as conn:
            accepted, row = api.submit_result(
                conn, "op-1", "exec-B", 2, "rcpt-XYZ", 1)
        self.assertTrue(accepted)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["receipt_id"], "rcpt-XYZ")

        # 终态后任何迟到提交都不能覆盖
        with api.closing(api.get_conn()) as conn:
            accepted, row = api.submit_result(
                conn, "op-1", "exec-B", 2, "rcpt-TAMPER", 9)
        self.assertFalse(accepted)
        self.assertEqual(row["receipt_id"], "rcpt-XYZ")
        self.assertEqual(row["applied_count"], 1)

    def test_no_receipt_until_succeeded(self):
        with api.closing(api.get_conn()) as conn:
            api.create_ramp(conn, self.payload)
            row = conn.execute(
                "SELECT * FROM ramps WHERE operation_id = ?",
                ("op-1",)).fetchone()
        self.assertNotIn("receipt", api.ramp_public(row, True))


class InstrumentIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        instrument.DB_PATH = os.path.join(self.tmp.name, "instrument.db")
        instrument.APPLY_DELAY_SECONDS = 0
        instrument.init_db()
        self.body = {"operationId": "op-9", "magnetId": "M9",
                     "targetMilliamps": 321, "fenceToken": 1}

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_once_then_idempotent(self):
        s1, b1 = instrument.apply_op(dict(self.body))
        self.assertEqual(s1, 201)
        self.assertTrue(b1["reapplied"])
        self.assertEqual(b1["appliedCount"], 1)
        receipt = b1["receiptId"]

        # 网络重试 / 续领（更高 fence）重复调用
        for fence in (1, 2, 3):
            s2, b2 = instrument.apply_op({**self.body, "fenceToken": fence})
            self.assertEqual(s2, 200)
            self.assertFalse(b2["reapplied"])
            self.assertEqual(b2["receiptId"], receipt)
            self.assertEqual(b2["appliedCount"], 1)

    def test_conflicting_params(self):
        instrument.apply_op(dict(self.body))
        s, b = instrument.apply_op({**self.body, "targetMilliamps": 999})
        self.assertEqual(s, 409)
        self.assertNotIn("receiptId", b)


if __name__ == "__main__":
    unittest.main()
