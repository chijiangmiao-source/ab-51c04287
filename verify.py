#!/usr/bin/env python3
"""One-shot verification for the ramp control station.

Stages, in order:
  1. code tests   — the unittest suite (idempotency, fencing, crash convergence)
  2. build check  — byte-compile every module; if Docker is available, also
                    validate the image build and compose config
  3. HTTP smoke   — boot the real API + instrument + executor processes and
                    exercise the public contract over HTTP
  4. crash drill  — inject CRASH_AFTER_APPLY so the executor dies after the
                    instrument applied but before commit; a restarted executor
                    must converge the operation to `succeeded` with the SAME
                    receiptId and appliedCount == 1

Exit code 0 only if every stage passes; non-zero otherwise.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
_failures: list[str] = []


def report(ok: bool, label: str, detail: str = ""):
    print(f"  [{PASS if ok else FAIL}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


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
    except urllib.error.URLError as exc:
        return None, {"error": {"code": "unreachable", "message": str(exc)}}


def wait_health(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", f"{url}/healthz")
        if status == 200:
            return True
        time.sleep(0.2)
    return False


def wait_status(api: str, op_id: str, want: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        _, body = http("GET", f"{api}/api/ramps/{op_id}")
        if body.get("status") == want:
            return body
        time.sleep(0.25)
    return body


# ---------------------------------------------------------------- stages ----

def stage_tests() -> bool:
    print("\n== stage 1/4: code tests ==")
    proc = subprocess.run(
        [PY, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    for line in tail:
        print(f"    {line}")
    ok = proc.returncode == 0
    report(ok, "unittest suite", f"exit={proc.returncode}")
    return ok


def stage_build() -> bool:
    print("\n== stage 2/4: build checks ==")
    ok = True
    proc = subprocess.run(
        [PY, "-m", "compileall", "-q", "app", "tests", "verify.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    report(proc.returncode == 0, "byte-compile all modules",
           proc.stderr.strip() or "compileall clean")
    ok &= proc.returncode == 0

    docker = shutil.which("docker")
    if docker:
        build = subprocess.run(
            [docker, "build", "-q", "-t", "magnet-ramp:verify", "."],
            cwd=ROOT, capture_output=True, text=True,
        )
        report(build.returncode == 0, "docker image build",
               (build.stderr or build.stdout).strip().splitlines()[-1]
               if (build.stderr or build.stdout).strip() else "image built")
        ok &= build.returncode == 0
        cfg = subprocess.run(
            [docker, "compose", "-f", "compose.yaml", "config", "-q"],
            cwd=ROOT, capture_output=True, text=True,
        )
        report(cfg.returncode == 0, "compose config validates",
               cfg.stderr.strip() or "compose.yaml valid")
        ok &= cfg.returncode == 0
    else:
        print("    (docker not available here; image/compose checks run in CI)")
    return ok


class Stack:
    """Real OS processes for api/instrument/executor on throwaway ports+DBs."""

    def __init__(self, tmp: str, crash_ops: str = ""):
        self.tmp = tmp
        self.api_port, self.inst_port = 18080, 18100
        self.api = f"http://127.0.0.1:{self.api_port}"
        self.inst = f"http://127.0.0.1:{self.inst_port}"
        self._crash_ops = crash_ops
        self.procs: list[subprocess.Popen] = []

    def _spawn(self, module: str, env_extra: dict, tag: str) -> subprocess.Popen:
        env = dict(os.environ, **env_extra)
        log = open(os.path.join(self.tmp, f"{tag}.log"), "ab")
        proc = subprocess.Popen(
            [PY, "-m", f"app.{module}"], cwd=ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
        self.procs.append(proc)
        return proc

    def start_services(self):
        self._spawn("instrument", {
            "INSTRUMENT_HOST": "127.0.0.1", "INSTRUMENT_PORT": str(self.inst_port),
            "INSTRUMENT_DB": os.path.join(self.tmp, "instrument.db"),
        }, "instrument")
        self._spawn("api", {
            "API_HOST": "127.0.0.1", "API_PORT": str(self.api_port),
            "API_DB": os.path.join(self.tmp, "api.db"),
            "INSTRUMENT_URL": self.inst, "LEASE_SECONDS": "2",
        }, "api")

    def start_executor(self, tag="executor"):
        return self._spawn("executor", {
            "EXECUTOR_API_URL": self.api, "INSTRUMENT_URL": self.inst,
            "EXECUTOR_ID": f"exec-{tag}", "CLAIM_WAIT_SECONDS": "0.3",
            "RENEW_INTERVAL_SECONDS": "0.5", "LEASE_SECONDS": "2",
            "CRASH_AFTER_APPLY": self._crash_ops,
        }, tag)

    def stop_all(self):
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def stage_smoke() -> bool:
    print("\n== stage 3/4: HTTP smoke (real processes) ==")
    ok = True
    with tempfile.TemporaryDirectory(prefix="ramp-smoke-") as tmp:
        stack = Stack(tmp)
        stack.start_services()
        try:
            report(wait_health(stack.api) and wait_health(stack.inst),
                   "healthz reachable on api + instrument")
            stack.start_executor()
            time.sleep(0.5)

            st, body = http("POST", f"{stack.api}/api/ramps", {
                "operationId": "smoke-1", "magnetId": "magnet-a",
                "targetMilliAmps": 1500})
            report(st == 201 and body.get("status") == "queued",
                   "POST /api/ramps -> 201 queued", f"got {st}")

            st, body2 = http("POST", f"{stack.api}/api/ramps", {
                "operationId": "smoke-1", "magnetId": "magnet-a",
                "targetMilliAmps": 1500})
            report(st == 200 and body2 == body, "same id + same params replays original")

            st, body3 = http("POST", f"{stack.api}/api/ramps", {
                "operationId": "smoke-1", "magnetId": "magnet-a",
                "targetMilliAmps": 999})
            report(st == 409 and "receipt" not in body3,
                   "same id + different params -> 409 without receipt", f"got {st}")

            st, body4 = http("POST", f"{stack.api}/api/ramps", {
                "operationId": "bad id!", "targetMilliAmps": "lots"})
            fields = {f.get("field") for f in body4.get("error", {}).get("fields", [])}
            report(st == 422 and {"operationId", "magnetId", "targetMilliAmps"} <= fields,
                   "field errors -> locatable 422", f"fields={sorted(fields)}")

            final = wait_status(stack.api, "smoke-1", "succeeded")
            receipt = final.get("receipt", {})
            report(final.get("status") == "succeeded"
                   and receipt.get("receiptId") == "rcpt-smoke-1"
                   and receipt.get("appliedCount") == 1,
                   "smoke-1 succeeded with receipt appliedCount=1",
                   f"final={final.get('status')} receipt={receipt}")
            ok &= final.get("status") == "succeeded"

            st, _ = http("GET", f"{stack.api}/api/ramps/never-existed")
            report(st == 404, "unknown operationId -> 404")
        finally:
            stack.stop_all()
    return ok


def stage_crash_drill() -> bool:
    print("\n== stage 4/4: crash-after-apply convergence drill ==")
    ok = True
    with tempfile.TemporaryDirectory(prefix="ramp-crash-") as tmp:
        stack = Stack(tmp, crash_ops="drill-1")
        stack.start_services()
        try:
            if not (wait_health(stack.api) and wait_health(stack.inst)):
                report(False, "services healthy before drill")
                return False
            executor = stack.start_executor(tag="executor-1")
            time.sleep(0.5)

            http("POST", f"{stack.api}/api/ramps", {
                "operationId": "drill-1", "magnetId": "magnet-z",
                "targetMilliAmps": 2200})

            # The executor applies on the instrument, then os._exit(1)s before
            # the commit. Wait for the crash and confirm the process died.
            try:
                executor.wait(timeout=20)
                crashed = executor.returncode not in (None, 0)
            except subprocess.TimeoutExpired:
                crashed = False
            report(crashed, "executor crashed after apply (before commit)",
                   f"exit={executor.returncode}")
            ok &= crashed

            mid = wait_status(stack.api, "drill-1", "running", timeout=5)
            report(mid.get("status") == "running" and "receipt" not in mid,
                   "after crash: still running, no receipt leaked")

            _, ledger = http("GET", f"{stack.inst}/instrument/applications/drill-1")
            receipt_id = ledger.get("receiptId")
            report(ledger.get("appliedCount") == 1 and bool(receipt_id),
                   "instrument ledger: applied exactly once", f"receipt={receipt_id}")

            # Supervision restarts the executor (compose: restart: unless-stopped).
            stack.start_executor(tag="executor-2")
            final = wait_status(stack.api, "drill-1", "succeeded", timeout=25)
            receipt = final.get("receipt", {})
            report(final.get("status") == "succeeded",
                   "restarted executor converges drill-1 to succeeded",
                   f"final={final.get('status')}")
            report(receipt.get("receiptId") == receipt_id,
                   "receiptId unchanged after recovery",
                   f"{receipt.get('receiptId')} == {receipt_id}")
            report(receipt.get("appliedCount") == 1,
                   "appliedCount is exactly 1 (no double apply)",
                   f"appliedCount={receipt.get('appliedCount')}")
            ok &= (final.get("status") == "succeeded"
                   and receipt.get("receiptId") == receipt_id
                   and receipt.get("appliedCount") == 1)

            _, ledger2 = http("GET", f"{stack.inst}/instrument/applications/drill-1")
            report(ledger2.get("appliedCount") == 1,
                   "instrument ledger still shows appliedCount=1")
            ok &= ledger2.get("appliedCount") == 1
        finally:
            stack.stop_all()
    return ok


def main() -> int:
    print("ramp-station verify — tests, build, smoke, crash drill")
    results = [stage_tests(), stage_build(), stage_smoke(), stage_crash_drill()]
    print("\n== summary ==")
    if all(results) and not _failures:
        print("ALL STAGES PASSED")
        return 0
    for failure in _failures:
        print(f"  failed: {failure}")
    print("VERIFY FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
