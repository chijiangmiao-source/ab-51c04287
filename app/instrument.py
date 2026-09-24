"""仪器模拟端（独立提交、独立数据库）。

- POST /apply：按 operationId 幂等落账。
  * 首次：真正“施加”一次，applied_count=1，返回稳定 receiptId
  * 同标识同参重试：原账返回，施加次数不增加
  * 同标识异参：409
- GET  /ledger/{operationId}：查询落账（供核验）
- GET  /healthz：存活探测
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DB_PATH = os.environ.get("INSTRUMENT_DB_PATH", "/data/instrument.db")
APPLY_DELAY_SECONDS = float(os.environ.get("APPLY_DELAY_SECONDS", "0.05"))


def now_ms():
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
            CREATE TABLE IF NOT EXISTS ledger (
                operation_id  TEXT PRIMARY KEY,
                magnet_id     TEXT NOT NULL,
                target_ma     INTEGER NOT NULL,
                receipt_id    TEXT NOT NULL,
                applied_count INTEGER NOT NULL,
                last_fence    INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL
            )
        """)


def stable_receipt_id(op_id, magnet_id, target_ma):
    digest = hashlib.sha256(
        f"{op_id}|{magnet_id}|{target_ma}".encode("ascii")
    ).hexdigest()
    return f"rcpt-{digest[:24]}"


def apply_op(body):
    """返回 (status, response_body)。"""
    try:
        op_id = body["operationId"]
        magnet_id = body["magnetId"]
        target_ma = body["targetMilliamps"]
        fence = int(body["fenceToken"])
    except (KeyError, TypeError, ValueError):
        return 422, {"error": {"code": "validation_failed",
                               "message": "缺少 operationId/magnetId/"
                                          "targetMilliamps/fenceToken"}}
    if not (isinstance(op_id, str) and isinstance(magnet_id, str)
            and isinstance(target_ma, int) and not isinstance(target_ma, bool)):
        return 422, {"error": {"code": "validation_failed",
                               "message": "字段类型错误"}}
    receipt_id = stable_receipt_id(op_id, magnet_id, target_ma)
    ts = now_ms()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM ledger WHERE operation_id = ?", (op_id,)
        ).fetchone()
        if row is not None:
            if (row["magnet_id"] != magnet_id
                    or row["target_ma"] != target_ma):
                conn.execute("ROLLBACK")
                return 409, {"error": {
                    "code": "conflict",
                    "message": "operationId 已以不同参数落账",
                    "existing": {
                        "operationId": row["operation_id"],
                        "magnetId": row["magnet_id"],
                        "targetMilliamps": row["target_ma"],
                    }}}
            # 幂等：重试/续领/崩溃恢复都不重复施加
            conn.execute(
                "UPDATE ledger SET last_fence = ?, updated_at = ? "
                "WHERE operation_id = ?",
                (max(fence, row["last_fence"]), ts, op_id))
            conn.execute("COMMIT")
            row = conn.execute(
                "SELECT * FROM ledger WHERE operation_id = ?", (op_id,)
            ).fetchone()
            return 200, receipt_body(row, reapplied=False)

        # 模拟真实施加耗时（在事务外亦可；此处极短）
        time.sleep(APPLY_DELAY_SECONDS)
        conn.execute(
            "INSERT INTO ledger (operation_id, magnet_id, target_ma, "
            "receipt_id, applied_count, last_fence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (op_id, magnet_id, target_ma, receipt_id, fence, ts, ts))
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT * FROM ledger WHERE operation_id = ?", (op_id,)
        ).fetchone()
        return 201, receipt_body(row, reapplied=True)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def receipt_body(row, reapplied):
    return {
        "operationId": row["operation_id"],
        "receiptId": row["receipt_id"],
        "appliedCount": row["applied_count"],
        "reapplied": reapplied,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "InstrumentSim/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[instrument] %s - %s\n"
                         % (self.address_string(), fmt % args))

    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path.startswith("/ledger/"):
            op_id = path[len("/ledger/"):]
            with closing(get_conn()) as conn:
                row = conn.execute(
                    "SELECT * FROM ledger WHERE operation_id = ?", (op_id,)
                ).fetchone()
            if row is None:
                self._send_json(404, {"error": {"code": "not_found",
                                                "message": "无落账记录"}})
                return
            self._send_json(200, {
                "operationId": row["operation_id"],
                "magnetId": row["magnet_id"],
                "targetMilliamps": row["target_ma"],
                "receiptId": row["receipt_id"],
                "appliedCount": row["applied_count"],
                "lastFence": row["last_fence"],
            })
            return
        self._send_json(404, {"error": {"code": "not_found",
                                        "message": "未知路径"}})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/apply":
            self._send_json(404, {"error": {"code": "not_found",
                                            "message": "未知路径"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": {"code": "bad_request",
                                            "message": "非法 JSON"}})
            return
        try:
            status, out = apply_op(body)
        except sqlite3.Error as e:
            status, out = 500, {"error": {"code": "internal_error",
                                          "message": str(e)}}
        self._send_json(status, out)


def main():
    init_db()
    port = int(os.environ.get("INSTRUMENT_PORT", "8081"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"[instrument] listening on :{port} db={DB_PATH}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
