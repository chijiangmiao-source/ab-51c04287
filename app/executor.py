"""斜坡执行器。

工作循环：
1. POST /internal/leases 领取任务（得到单调递增的 fenceToken 与有限期租约）
2. 后台心跳按 TTL/3 续租
3. 调用仪器模拟端 POST /apply（仪器按 operationId 幂等落账）
4. 带 fenceToken 提交 /internal/results；若已被更新的领取者取代
   （accepted=false），安静退出，绝不重复施加、绝不覆盖新结果

崩溃注入（仅用于验证恢复语义）：
- CRASH_AFTER_APPLY=<operationId>：仪器已施加但结果未提交时 os._exit
- CRASH_BEFORE_APPLY=<operationId>：施加前退出（租约到期后被续领）

所有网络调用均带有限重试；依赖 API fencing 与仪器幂等保证恰好一次施加。
"""
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("API_URL", "http://api:8080").rstrip("/")
INSTRUMENT_URL = os.environ.get(
    "INSTRUMENT_URL", "http://instrument:8081").rstrip("/")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "0.5"))
CRASH_AFTER_APPLY = os.environ.get("CRASH_AFTER_APPLY", "")
CRASH_BEFORE_APPLY = os.environ.get("CRASH_BEFORE_APPLY", "")
# 崩溃标记落盘目录：同一容器/工作目录重启后只崩溃一次，避免崩溃-重启死循环
CRASH_STATE_DIR = os.environ.get("CRASH_STATE_DIR", "/tmp")

OWNER = "executor-%s-%d-%04x" % (
    socket.gethostname(), os.getpid(), random.randint(0, 0xFFFF))


def crash_once(tag, op_id):
    """对指定操作在进程生命周期内只注入一次崩溃。

    tag: 'before-apply' | 'after-apply'。重启后的新进程看到标记文件，
    将走正常恢复路径（续领租约、仪器幂等返回、提交结果）。
    """
    flag = {"after-apply": CRASH_AFTER_APPLY,
            "before-apply": CRASH_BEFORE_APPLY}.get(tag, "")
    if not flag or op_id != flag:
        return
    marker = os.path.join(CRASH_STATE_DIR, f"ramp-crash-{tag}-{op_id}")
    if os.path.exists(marker):
        sys.stderr.write(
            f"[exec] crash marker present for {op_id}, running recovery\n")
        return
    os.makedirs(CRASH_STATE_DIR, exist_ok=True)
    with open(marker, "w", encoding="ascii") as fh:
        fh.write(OWNER + "\n")
    sys.stderr.write(f"[exec] CRASH injected ({tag}): {op_id}\n")
    sys.stderr.flush()
    sys.stdout.flush()
    os._exit(42)


def http_json(method, url, payload=None, retries=5, timeout=5,
              allow_status=()):
    """带退避重试的 JSON HTTP 调用。连接错误/5xx 重试；4xx 立即返回。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, {}
                return resp.status, json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in allow_status:
                try:
                    return e.code, json.loads(body)
                except json.JSONDecodeError:
                    return e.code, {}
            if 500 <= e.code < 600 and attempt < retries - 1:
                last_err = e
            else:
                try:
                    return e.code, json.loads(body)
                except json.JSONDecodeError:
                    raise RuntimeError(f"HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError, OSError) as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(min(0.1 * (2 ** attempt), 2.0)
                       + random.random() * 0.05)
    raise RuntimeError(f"调用 {url} 重试耗尽: {last_err}")


class Heartbeat(threading.Thread):
    def __init__(self, task):
        super().__init__(daemon=True)
        self.task = task
        self.stop_event = threading.Event()
        self.lost = False

    def run(self):
        interval = max(0.2, float(self.task["leaseTtlSeconds"]) / 3.0)
        while not self.stop_event.wait(interval):
            try:
                _, body = http_json(
                    "POST", f"{API_URL}/internal/heartbeat",
                    {"operationId": self.task["operationId"],
                     "owner": OWNER,
                     "fenceToken": self.task["fenceToken"]},
                    retries=3, timeout=3)
            except Exception as e:
                sys.stderr.write(f"[exec] heartbeat error: {e}\n")
                continue
            if not body.get("renewed"):
                # 租约已被更新的执行者以更高 fencing token 接管
                self.lost = True
                self.stop_event.set()

    def stop(self):
        self.stop_event.set()


def handle_task(task):
    op_id = task["operationId"]
    fence = task["fenceToken"]
    sys.stderr.write(
        f"[exec] leased {op_id} fence={fence} "
        f"target={task['targetMilliamps']}mA\n")

    if CRASH_BEFORE_APPLY and op_id == CRASH_BEFORE_APPLY:
        crash_once("before-apply", op_id)

    hb = Heartbeat(task)
    hb.start()
    try:
        # 仪器端按 operationId 幂等：网络重试/崩溃续领都不会重复施加
        status, receipt = http_json(
            "POST", f"{INSTRUMENT_URL}/apply",
            {"operationId": op_id,
             "magnetId": task["magnetId"],
             "targetMilliamps": task["targetMilliamps"],
             "fenceToken": fence},
            retries=6, timeout=10)
        if status not in (200, 201):
            raise RuntimeError(f"仪器拒绝: {status} {receipt}")
        receipt_id = receipt["receiptId"]
        applied_count = receipt["appliedCount"]
        sys.stderr.write(
            f"[exec] instrument receipt={receipt_id} count={applied_count} "
            f"reapplied={receipt.get('reapplied')}\n")

        if CRASH_AFTER_APPLY and op_id == CRASH_AFTER_APPLY:
            # 仪器已应用、状态尚未提交 —— 模拟进程崩溃/掉电
            crash_once("after-apply", op_id)

        # 带 fencing token 提交；迟到执行者（更低/过期 token）会被拒绝
        status, body = http_json(
            "POST", f"{API_URL}/internal/results",
            {"operationId": op_id, "owner": OWNER, "fenceToken": fence,
             "receiptId": receipt_id, "appliedCount": applied_count},
            retries=6, timeout=5)
        if body.get("accepted"):
            sys.stderr.write(f"[exec] committed {op_id}: {receipt_id}\n")
        else:
            sys.stderr.write(
                f"[exec] stale fence {fence} for {op_id}, "
                f"newer executor owns it; not overwriting\n")
    finally:
        hb.stop()


def wait_healthy(url, attempts=60):
    for _ in range(attempts):
        try:
            status, _ = http_json("GET", f"{url}/healthz", retries=1,
                                  timeout=2)
            if status == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"服务未就绪: {url}")


def main():
    sys.stderr.write(f"[exec] starting owner={OWNER} api={API_URL} "
                     f"instrument={INSTRUMENT_URL}\n")
    wait_healthy(API_URL)
    wait_healthy(INSTRUMENT_URL)
    while True:
        try:
            _, body = http_json(
                "POST", f"{API_URL}/internal/leases",
                {"owner": OWNER}, retries=3, timeout=5)
            task = body.get("task")
            if task is None:
                time.sleep(POLL_INTERVAL)
                continue
            handle_task(task)
        except Exception as e:
            sys.stderr.write(f"[exec] loop error: {e}\n")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
