# 低温磁体斜坡指令服务（幂等 + fencing token 崩溃恢复）

三个独立进程：

| 组件 | 入口 | 端口（容器内） | 职责 |
|---|---|---|---|
| API | `app/api.py` | 8080 | 指令状态机 `queued → running → succeeded`、有期限租约、fencing token |
| 执行器 | `app/executor.py` | — | 领任务 → 心跳续租 → 调仪器 → 带 token 提交结果 |
| 仪器模拟端 | `app/instrument.py` | 8081 | 独立 SQLite 落账，按 `operationId` 幂等，稳定 `receiptId` |

## 正确性要点

- **调用方重试安全**：`POST /api/ramps` 同标识同参返回原命令（200/201），异参 `409`，字段错误 `422`（错误带 JSON 指针字段定位）；失败响应不夹带 receipt。
- **恰好施加一次**：仪器端以 `operationId` 为主键落账，重试/续领只返回原账，`appliedCount` 恒为 1，`receiptId = sha256(operationId|magnetId|targetMilliamps)` 稳定。
- **崩溃收敛**：执行器在“仪器已应用、状态未提交”时退出（`CRASH_AFTER_APPLY` 注入，`os._exit(42)`）；租约 TTL 到期后任务被续领，每次续领 **fencing token 单调递增**；新执行者幂等取得原 receiptId 并提交成功，迟到执行者的旧 token 提交被拒绝（`accepted=false`），不能覆盖新结果。
- 租约由心跳（TTL/3）续租；终态 `succeeded` 后任何提交都不可改。

## HTTP 接口

```
POST /api/ramps                 # {operationId, magnetId, targetMilliamps}
GET  /api/ramps/{operationId}   # succeeded 时含 receipt{receiptId, appliedCount}
GET  /healthz
POST /internal/leases  {"owner": "..."}                 # 执行器用
POST /internal/results {operationId,owner,fenceToken,receiptId,appliedCount}
POST /internal/heartbeat {operationId,owner,fenceToken}
POST /apply                 # 仪器端；GET /ledger/{operationId} 可查落账
```

## 启动（Docker Compose）

```bash
API_HOST_PORT=18080 INSTRUMENT_HOST_PORT=18081 \
CRASH_AFTER_APPLY=op-crash-after-apply LEASE_TTL_SECONDS=10 \
docker compose up --build
curl -s localhost:18080/healthz
```

宿主端口由 `API_HOST_PORT` / `INSTRUMENT_HOST_PORT` 配置（默认 18080/18081）。

## 一键验证

```bash
python3 verify.py            # 有 docker 走 compose build/up；无 docker 走本地进程
python3 verify.py --local    # 强制本地
python3 verify.py --docker   # 强制 compose
```

verify 依次执行：单元测试 → 构建检查 → `/healthz` 与 HTTP 冒烟（幂等/409/422/无回执泄漏）
→ 应用后崩溃恢复，确认最终 `succeeded`、`receiptId` 不变、`appliedCount == 1`，
以退出码报告结果（0 全通过）。
