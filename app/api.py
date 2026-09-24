"""API 服务：斜坡指令状态机（queued/running/succeeded）。

- POST /api/ramps：幂等创建；同标识同参返回原命令，异参 409，字段错误 422
- GET  /api/ramps/{operationId}：状态与回执；succeeded 才带 receipt
- POST /internal/leases：执行器领取任务（有期限租约，fencing token 递增）
- POST /internal/results：执行器带 token 回执（迟到/过期 token 被拒绝）
- GET  /healthz：存活探测
"""
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import validate_ramp_payload  # noqa: E402

DB_PATH = os.environ.get("API_DB_PATH", "/data/api.db")
LEASE_TTL_SECONDS = float(os.environ.get("LEASE_TTL_SECONDS", "10"))


def now_ms() -> int:
    return int(time.time() * 1000)


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(get_conn()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ramps (
                operation_id   TEXT PRIMARY KEY,
                magnet_id      TEXT NOT NULL,
                target_ma      INTEGER NOT NULL,
                status         TEXT NOT NULL,
                lease_owner    TEXT,
                lease_expires  INTEGER,
                fence          INTEGER NOT NULL DEFAULT 0,
                receipt_id     TEXT,
                applied_count  INTEGER,
                completed_at   INTEGER,
                created_at     INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ramps_status_lease
            ON ramps(status, lease_expires)
        """)


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        self.status = status
        self.message = message
        self.extra = extra
        super().__init__(message)


def create_ramp(conn, payload):
    """插入或幂等返回。返回 (http_status, body)。"""
    op_id = payload["operationId"]
    ts = now_ms()
    # SQLite 单行 UPSERT 的冲突检测需先查后插；用 IMMEDIATE 事务串行化。
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM ramps WHERE operation_id = ?", (op_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO ramps (operation_id, magnet_id, target_ma, "
                "status, fence, created_at) VALUES (?, ?, ?, 'queued', 0, ?)",
                (op_id, payload["magnetId"], payload["targetMilliamps"], ts),
            )
            conn.execute("COMMIT")
            row = conn.execute(
                "SELECT * FROM ramps WHERE operation_id = ?", (op_id,)
            ).fetchone()
            return 201, ramp_public(row)
        if (row["magnet_id"] != payload["magnetId"]
                or row["target_ma"] != payload["targetMilliamps"]):
            raise ApiError(
                409, "operationId 已存在但参数不一致",
                existing={
                    "operationId": row["operation_id"],
                    "magnetId": row["magnet_id"],
                    "targetMilliamps": row["target_ma"],
                },
            )
        # 同参重试：返回原命令（状态随之演进，不是快照）
        return 200, ramp_public(row)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def ramp_public(row, include_receipt=False):
    body = {
        "operationId": row["operation_id"],
        "magnetId": row["magnet_id"],
        "targetMilliamps": row["target_ma"],
        "status": row["status"],
    }
    if include_receipt and row["status"] == "succeeded":
        # 只有成功终态才夹带回执，失败/进行中响应绝不出现 receipt
        body["receipt"] = {
            "receiptId": row["receipt_id"],
            "appliedCount": row["applied_count"],
        }
    return body


def acquire_lease(conn, owner):
    """领取一个可执行任务：queued 或租约已过期的 running。

    每次发放都使 fence 单调递增，旧执行器之后无法提交结果。
    """
    ts = now_ms()
    ttl_ms = int(LEASE_TTL_SECONDS * 1000)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM ramps WHERE status = 'queued' "
            "OR (status = 'running' AND lease_expires < ?) "
            "ORDER BY created_at LIMIT 1",
            (ts,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        new_fence = row["fence"] + 1
        conn.execute(
            "UPDATE ramps SET status = 'running', lease_owner = ?, "
            "lease_expires = ?, fence = ? WHERE operation_id = ?",
            (owner, ts + ttl_ms, new_fence, row["operation_id"]),
        )
        conn.execute("COMMIT")
        return {
            "operationId": row["operation_id"],
            "magnetId": row["magnet_id"],
            "targetMilliamps": row["target_ma"],
            "fenceToken": new_fence,
            "leaseTtlSeconds": LEASE_TTL_SECONDS,
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise


def submit_result(conn, op_id, owner, fence, receipt_id, applied_count):
    """带 fencing token 的结果提交。返回 (accepted: bool, row)。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM ramps WHERE operation_id = ?", (op_id,)
        ).fetchone()
        accept = (
            row is not None
            and row["status"] == "running"
            and row["lease_owner"] == owner
            and row["fence"] == fence
        )
        if accept:
            conn.execute(
                "UPDATE ramps SET status = 'succeeded', receipt_id = ?, "
                "applied_count = ?, completed_at = ? WHERE operation_id = ?",
                (receipt_id, applied_count, now_ms(), op_id),
            )
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT * FROM ramps WHERE operation_id = ?", (op_id,)
        ).fetchone()
        return bool(accept), row
    except Exception:
        conn.execute("ROLLBACK")
        raise


def heartbeat(conn, op_id, owner, fence):
    """续租：仅当前持有者用当前 token 可续，fence 不变。"""
    ts = now_ms()
    ttl_ms = int(LEASE_TTL_SECONDS * 1000)
    cur = conn.execute(
        "UPDATE ramps SET lease_expires = ? WHERE operation_id = ? "
        "AND status = 'running' AND lease_owner = ? AND fence = ?",
        (ts + ttl_ms, op_id, owner, fence),
    )
    return cur.rowcount == 1


class Handler(BaseHTTPRequestHandler):
    server_version = "RampAPI/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[api] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- 工具 ----
    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "缺少 JSON 请求体")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求体不是合法 UTF-8 JSON")
        if not isinstance(body, dict):
            raise ApiError(422, "请求体必须是 JSON 对象",
                          errors=[{"field": "", "message": "根节点必须是对象"}])
        return body

    def _error(self, status, message, **extra):
        payload = {"error": {"code": http_code_name(status), "message": message}}
        payload["error"].update(extra)
        # 失败响应绝不夹带成功回执
        self._send_json(status, payload)

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path.startswith("/api/ramps/"):
            op_id = path[len("/api/ramps/"):]
            if "/" in op_id or not op_id:
                self._error(404, "未知路径")
                return
            with closing(get_conn()) as conn:
                row = conn.execute(
                    "SELECT * FROM ramps WHERE operation_id = ?", (op_id,)
                ).fetchone()
            if row is None:
                self._error(404, "operationId 不存在")
                return
            self._send_json(200, ramp_public(row, include_receipt=True))
            return
        self._error(404, "未知路径")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/ramps":
                body = self._read_json()
                clean, errors = validate_ramp_payload(body)
                if errors:
                    self._error(422, "字段校验失败", errors=errors)
                    return
                with closing(get_conn()) as conn:
                    status, out = create_ramp(conn, clean)
                self._send_json(status, out)
                return

            if path == "/internal/leases":
                body = self._read_json()
                owner = body.get("owner")
                if not isinstance(owner, str) or not owner:
                    self._error(422, "缺少执行器标识 owner",
                                errors=[{"field": "/owner",
                                         "message": "必须是非空字符串"}])
                    return
                with closing(get_conn()) as conn:
                    task = acquire_lease(conn, owner)
                if task is None:
                    self._send_json(200, {"task": None})
                else:
                    self._send_json(200, {"task": task})
                return

            if path == "/internal/results":
                body = self._read_json()
                required = ("operationId", "owner", "fenceToken",
                            "receiptId", "appliedCount")
                missing = [f"/{k}" for k in required if k not in body]
                if missing:
                    self._error(422, "缺少必填字段",
                                errors=[{"field": f, "message": "必填"}
                                        for f in missing])
                    return
                try:
                    fence = int(body["fenceToken"])
                    applied_count = int(body["appliedCount"])
                except (TypeError, ValueError):
                    self._error(422, "fenceToken/appliedCount 必须是整数",
                                errors=[{"field": "/fenceToken",
                                         "message": "必须是整数"}])
                    return
                with closing(get_conn()) as conn:
                    accepted, row = submit_result(
                        conn, str(body["operationId"]), str(body["owner"]),
                        fence, str(body["receiptId"]), applied_count)
                if not accepted:
                    # fencing 失败：拒绝迟到执行器覆盖，200 + accepted=false
                    self._send_json(200, {"accepted": False,
                                          "status": row["status"] if row else None})
                else:
                    self._send_json(200, {"accepted": True,
                                          "status": "succeeded"})
                return

            if path == "/internal/heartbeat":
                body = self._read_json()
                try:
                    op_id = str(body["operationId"])
                    owner = str(body["owner"])
                    fence = int(body["fenceToken"])
                except (KeyError, TypeError, ValueError):
                    self._error(422, "需要 operationId/owner/fenceToken")
                    return
                with closing(get_conn()) as conn:
                    ok = heartbeat(conn, op_id, owner, fence)
                self._send_json(200, {"renewed": ok})
                return

            self._error(404, "未知路径")
        except ApiError as e:
            self._error(e.status, e.message, **e.extra)
        except (sqlite3.Error, KeyError, ValueError) as e:
            self._error(500, f"内部错误: {e}")


def http_code_name(status):
    return {400: "bad_request", 404: "not_found", 409: "conflict",
            422: "validation_failed", 500: "internal_error"}.get(
        status, "error")


def main():
    init_db()
    port = int(os.environ.get("API_PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"[api] listening on :{port} db={DB_PATH}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
