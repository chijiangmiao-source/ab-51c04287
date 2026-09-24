#!/usr/bin/env python3
"""verify：单次服务执行代码测试、构建检查、HTTP 冒烟与崩溃恢复核验。

用法:
    python3 verify.py                # 自动选择：有 docker 用 compose，否则本地进程
    python3 verify.py --local        # 强制本地进程模式（无需镜像/网络）
    python3 verify.py --docker       # 强制 docker compose 模式

核验内容：
  1. 全部单元测试
  2. 构建检查（docker compose build 或 py_compile）
  3. HTTP 冒烟：
     - /healthz 可探测
     - POST 新指令 201；同标识同参重试返回原命令；异参 409
     - 字段错误 422 且可定位到字段；失败响应不含成功回执
     - 轮询 GET 直到 succeeded，receiptId 稳定、appliedCount=1
  4. 应用后崩溃恢复：
     - 执行器在仪器已应用、状态未提交时退出
     - 续领使用递增 fencing token，最终 succeeded
     - receiptId 与仪器落账一致且保持不变，应用次数恰为 1
最终以退出码报告（0 全部通过，非 0 失败）。
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
CRASH_OP = "op-crash-after-apply"
LEASE_TTL = "2"


# ---------- 输出 ----------
class Report:
    def __init__(self):
        self.failures = []
        self.step = 0

    def check(self, name, cond, detail=""):
        if cond:
            print(f"  PASS  {name}")
        else:
            print(f"  FAIL  {name} {detail}")
            self.failures.append(f"{name} {detail}")

    def section(self, title):
        self.step += 1
        print(f"\n=== {self.step}. {title} ===")

    def info(self, msg):
        print(f"  ..   {msg}")

    def exit_code(self):
        print()
        if self.failures:
            print(f"VERIFY FAILED ({len(self.failures)} 项):")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print("VERIFY OK：全部检查通过")
        return 0


# ---------- HTTP ----------
def http(method, url, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"_raw": raw}


def wait_health(base, timeout=30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, body = http("GET", f"{base}/healthz", timeout=2)
            if status == 200:
                return True
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.3)
    raise RuntimeError(f"healthz 未就绪: {base} ({last})")


def wait_status(api_base, op_id, target, timeout=30.0):
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        status, body = http("GET", f"{api_base}/api/ramps/{op_id}")
        if status == 200 and body.get("status") == target:
            return body
        time.sleep(0.3)
    raise AssertionError(f"{op_id} 未在 {timeout}s 内到达 {target}: {body}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- 核验用例（对任意已启动的栈生效） ----------
def run_checks(api_base, instr_base, report):
    # healthz
    report.check("GET /healthz -> 200 ok", wait_health(api_base))
    report.check("仪器 /healthz -> 200 ok", wait_health(instr_base))

    # 1) 新建指令
    op = "op-normal-001"
    payload = {"operationId": op, "magnetId": "magnet:LT-7",
               "targetMilliamps": 1500}
    s1, b1 = http("POST", f"{api_base}/api/ramps", payload)
    report.check("POST 新指令返回 201", s1 == 201, f"got {s1} {b1}")
    report.check("初始状态 queued", b1.get("status") == "queued", str(b1))
    report.check("未成功响应不含 receipt", "receipt" not in b1, str(b1))

    # 同参快速重试若干次（模拟网络重试）
    sames = [http("POST", f"{api_base}/api/ramps", payload)
             for _ in range(3)]
    report.check(
        "同标识同参重试均 200 且返回原命令",
        all(s == 200 and b["operationId"] == op and
            b["targetMilliamps"] == 1500 for s, b in sames),
        str(sames))

    # 2) 异参复用 409，且失败响应不夹带回执
    s2, b2 = http("POST", f"{api_base}/api/ramps",
                  {**payload, "targetMilliamps": 1501})
    report.check("同标识异参返回 409", s2 == 409, f"got {s2} {b2}")
    report.check("409 体包含既有参数供定位",
                 b2.get("error", {}).get("existing", {})
                 .get("targetMilliamps") == 1500,
                 str(b2))
    report.check("409 失败响应不含 receipt", "receipt" not in b2
                 and "receiptId" not in b2, str(b2))

    # 3) 字段错误 422，可定位
    s3, b3 = http("POST", f"{api_base}/api/ramps",
                  {"operationId": "非ascii!", "magnetId": "",
                   "targetMilliamps": 3.5})
    fields = {e.get("field") for e in b3.get("error", {}).get("errors", [])}
    report.check("字段错误返回 422", s3 == 422, f"got {s3} {b3}")
    report.check("错误可定位到三个字段",
                 fields == {"/operationId", "/magnetId",
                            "/targetMilliamps"},
                 str(fields))
    report.check("422 失败响应不含 receipt", "receipt" not in b3
                 and "receiptId" not in b3, str(b3))

    s3b, b3b = http("POST", f"{api_base}/api/ramps", {"magnetId": "m"})
    report.check("缺字段返回 422 且定位 /operationId",
                 s3b == 422 and
                 any(e.get("field") == "/operationId"
                     for e in b3b.get("error", {}).get("errors", [])),
                 f"{s3b} {b3b}")

    # 4) 未知 operationId
    s4, b4 = http("GET", f"{api_base}/api/ramps/does-not-exist")
    report.check("未知 operationId 返回 404 且无回执",
                 s4 == 404 and "receipt" not in b4, f"{s4} {b4}")

    # 5) 执行到 succeeded，回执稳定，应用次数为 1
    done = wait_status(api_base, op, "succeeded", timeout=30)
    receipt = done.get("receipt", {})
    report.check("最终状态 succeeded", done["status"] == "succeeded", str(done))
    report.check("回执含稳定 receiptId",
                 isinstance(receipt.get("receiptId"), str)
                 and receipt["receiptId"].startswith("rcpt-"), str(receipt))
    report.check("appliedCount == 1", receipt.get("appliedCount") == 1,
                 str(receipt))

    sl, ledger = http("GET", f"{instr_base}/ledger/{op}")
    report.check("仪器落账 appliedCount == 1",
                 sl == 200 and ledger["appliedCount"] == 1, str(ledger))
    report.check("API 回执与仪器 receiptId 一致",
                 ledger.get("receiptId") == receipt.get("receiptId"),
                 f"{ledger} vs {receipt}")

    for _ in range(3):
        _, again = http("GET", f"{api_base}/api/ramps/{op}")
        same = again.get("receipt", {}).get("receiptId") == receipt["receiptId"]
        if not same:
            report.check("多次查询 receiptId 保持不变", False, str(again))
            break
    else:
        report.check("多次查询 receiptId 保持不变", True)

    # 6) 迟到/伪造结果提交不能覆盖
    s6, b6 = http("POST", f"{api_base}/internal/results",
                  {"operationId": op, "owner": "late-executor",
                   "fenceToken": 999999, "receiptId": "rcpt-FORGED",
                   "appliedCount": 42})
    _, after = http("GET", f"{api_base}/api/ramps/{op}")
    report.check("迟到提交被 fencing 拒绝",
                 b6.get("accepted") is False, str(b6))
    report.check("新结果未被覆盖（receiptId 与次数不变）",
                 after["receipt"]["receiptId"] == receipt["receiptId"]
                 and after["receipt"]["appliedCount"] == 1, str(after))


def run_crash_recovery(api_base, instr_base, report, already_submitted=False):
    """崩溃发生在调用方侧（执行器进程）；此处仅提交并轮询收敛结果。

    local 模式由本脚本负责重启第二个执行器进程；docker 模式由
    restart 策略自动重启执行器容器。
    """
    payload = {"operationId": CRASH_OP, "magnetId": "magnet:LT-7",
               "targetMilliamps": -800}
    if not already_submitted:
        s, b = http("POST", f"{api_base}/api/ramps", payload)
        report.check("崩溃用例指令已受理 (201/200)", s in (201, 200),
                     f"{s} {b}")

    # 先等仪器侧“恰好施加一次”（崩溃发生在应用之后）
    deadline = time.time() + 30
    ledger = None
    while time.time() < deadline:
        sl, ledger = http("GET", f"{instr_base}/ledger/{CRASH_OP}")
        if sl == 200:
            break
        time.sleep(0.3)
    report.check("仪器已施加一次（崩溃点：应用后、提交前）",
                 sl == 200 and ledger["appliedCount"] == 1, str(ledger))

    receipt_id = ledger["receiptId"] if sl == 200 else None

    # 崩溃后续领恢复（租约 TTL 很短），最终必须 succeeded
    done = wait_status(api_base, CRASH_OP, "succeeded", timeout=45)
    rec = done.get("receipt", {})
    report.check("崩溃恢复后最终 succeeded",
                 done.get("status") == "succeeded", str(done))
    report.check("恢复后 receiptId 与仪器原账一致且不变",
                 rec.get("receiptId") == receipt_id,
                 f"{rec} vs {receipt_id}")
    report.check("恢复后应用次数仍为 1（未重复施加）",
                 rec.get("appliedCount") == 1, str(rec))

    _, ledger2 = http("GET", f"{instr_base}/ledger/{CRASH_OP}")
    report.check("仪器落账应用次数始终为 1",
                 ledger2.get("appliedCount") == 1, str(ledger2))
    report.check("续领使用了递增 fencing token（lastFence >= 2）",
                 ledger2.get("lastFence", 0) >= 2, str(ledger2))


# ---------- 单元测试 ----------
def run_unit_tests(report):
    report.section("代码测试：单元测试")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, capture_output=True, text=True)
    passed = proc.returncode == 0
    if not passed:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
    report.check("全部单元测试通过", passed, f"exit={proc.returncode}")


# ---------- 本地进程模式 ----------
class LocalStack:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="ramp-verify-")
        self.api_port = free_port()
        self.instr_port = free_port()
        self.servers = []
        self.executors = []
        self.crash_state = os.path.join(self.tmp, "crash-state")
        os.makedirs(self.crash_state, exist_ok=True)

    def _start(self, role, env):
        full = dict(os.environ)
        full.update(env)
        log = open(os.path.join(self.tmp, f"{role}.log"), "ab")
        p = subprocess.Popen(
            [sys.executable, "-u", os.path.join(APP, "run.py"), role],
            cwd=ROOT, env=full, stdout=log, stderr=subprocess.STDOUT)
        self.servers.append(p)
        return p

    def build_check(self, report):
        report.section("构建检查（本地模式）：语法编译")
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", APP],
            capture_output=True, text=True)
        report.check("app/ 全部编译通过", proc.returncode == 0,
                     proc.stderr[-500:])
        # Dockerfile / compose 仍须存在且语法字段齐备
        dc = os.path.join(ROOT, "docker-compose.yml")
        df = os.path.join(ROOT, "Dockerfile")
        report.check("Dockerfile 存在", os.path.isfile(df))
        report.check("docker-compose.yml 存在", os.path.isfile(dc))

    def up(self):
        self._start("api", {
            "API_PORT": str(self.api_port),
            "API_DB_PATH": os.path.join(self.tmp, "api.db"),
            "LEASE_TTL_SECONDS": LEASE_TTL,
        })
        self._start("instrument", {
            "INSTRUMENT_PORT": str(self.instr_port),
            "INSTRUMENT_DB_PATH": os.path.join(self.tmp, "instrument.db"),
            "APPLY_DELAY_SECONDS": "0.05",
        })
        self.api_base = f"http://127.0.0.1:{self.api_port}"
        self.instr_base = f"http://127.0.0.1:{self.instr_port}"
        wait_health(self.api_base)
        wait_health(self.instr_base)
        self.start_executor(crash=False)

    def start_executor(self, crash):
        env = {
            "API_URL": self.api_base,
            "INSTRUMENT_URL": self.instr_base,
            "POLL_INTERVAL_SECONDS": "0.2",
            "CRASH_STATE_DIR": self.crash_state,
        }
        if crash:
            env["CRASH_AFTER_APPLY"] = CRASH_OP
        full = dict(os.environ)
        full.update(env)
        log = open(os.path.join(self.tmp, "executor.log"), "ab")
        p = subprocess.Popen(
            [sys.executable, "-u", os.path.join(APP, "run.py"), "executor"],
            cwd=ROOT, env=full, stdout=log, stderr=subprocess.STDOUT)
        self.executors.append(p)
        return p

    def stop_executors(self):
        for p in self.executors:
            p.terminate()
        for p in self.executors:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.executors = []

    def down(self):
        for p in self.servers + self.executors:
            p.terminate()
        for p in self.servers + self.executors:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------- Docker compose 模式 ----------
class DockerStack:
    def __init__(self):
        self.api_port = free_port()
        self.instr_port = free_port()
        self.name = f"rampverify_{int(time.time())}_{os.getpid()}"

    def _compose(self, *args):
        return subprocess.run(
            ["docker", "compose", "-p", self.name, *args],
            cwd=ROOT, capture_output=True, text=True)

    def build_check(self, report):
        report.section("构建检查：docker compose build")
        proc = self._compose("build")
        report.check("镜像构建成功", proc.returncode == 0,
                     (proc.stderr or proc.stdout)[-800:])

    def up(self):
        env = dict(os.environ)
        env.update({
            "API_HOST_PORT": str(self.api_port),
            "INSTRUMENT_HOST_PORT": str(self.instr_port),
            "LEASE_TTL_SECONDS": LEASE_TTL,
            "CRASH_AFTER_APPLY": CRASH_OP,
            "POLL_INTERVAL_SECONDS": "0.2",
        })
        proc = subprocess.run(
            ["docker", "compose", "-p", self.name, "up", "-d"],
            cwd=ROOT, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        self.api_base = f"http://127.0.0.1:{self.api_port}"
        self.instr_base = f"http://127.0.0.1:{self.instr_port}"
        wait_health(self.api_base, timeout=90)
        wait_health(self.instr_base, timeout=90)

    def start_executor(self, crash):
        # docker 模式只有一个执行器服务，崩溃后由 restart 策略自动重启
        return None

    def down(self):
        self._compose("down", "-v", "--remove-orphans")


def docker_available():
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--docker", action="store_true")
    args = ap.parse_args()

    use_docker = (not args.local and
                  (args.docker or docker_available()))
    if args.docker and not docker_available():
        print("请求了 --docker 但 docker 不可用", file=sys.stderr)
        return 2

    report = Report()
    print(f"运行模式: {'docker compose' if use_docker else '本地进程'}")

    run_unit_tests(report)

    stack = DockerStack() if use_docker else LocalStack()
    try:
        # 单元测试失败也继续构建/冒烟，收集全部失败后统一按退出码报告
        stack.build_check(report)
        report.section("启动服务并探测 /healthz")
        stack.up()
        report.info(f"api={stack.api_base} instrument={stack.instr_base}")

        report.section("HTTP 冒烟")
        run_checks(stack.api_base, stack.instr_base, report)

        report.section("应用后崩溃恢复核验")
        if isinstance(stack, LocalStack):
            # 先停常规执行器，再提交崩溃指令并换入带崩溃注入的执行器，
            # 避免常规执行器抢先完成该指令
            stack.stop_executors()
            crash_payload = {"operationId": CRASH_OP,
                             "magnetId": "magnet:LT-7",
                             "targetMilliamps": -800}
            cs, cb = http("POST", f"{stack.api_base}/api/ramps",
                          crash_payload)
            report.check("崩溃用例指令已受理 (201/200)", cs in (201, 200),
                         f"{cs} {cb}")
            victim = stack.start_executor(crash=True)
            deadline = time.time() + 20
            while time.time() < deadline:
                if victim.poll() is not None:
                    break
                time.sleep(0.2)
            report.check("执行器在应用后崩溃退出（exit code 42）",
                         victim.poll() == 42, f"rc={victim.poll()}")
            # 恢复进程：同一崩溃状态目录，看到标记后走恢复路径
            stack.start_executor(crash=True)
        run_crash_recovery(stack.api_base, stack.instr_base, report,
                           already_submitted=isinstance(stack, LocalStack))
    finally:
        stack.down()

    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
