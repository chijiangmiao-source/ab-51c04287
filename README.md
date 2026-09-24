# 低温磁体斜坡控制站（magnet-ramp）

斜坡指令的**幂等提交、租约执行与 fencing token 收敛**实现。网络重试或执行器崩溃不会导致斜坡被重复施加：仪器模拟端按 `operationId` 幂等落账，每次（重）领租约都使用递增 fencing token，迟到执行器无法覆盖新结果。

## 架构

```
调用方 ──POST /api/ramps──▶ api ──◀── claim/renew/complete ── executor
   │                        │                                  │
   └──GET /api/ramps/{id}──▶│                                  ▼
                            │                            instrument（仪器模拟端，
                            │                            独立进程/独立账本）
                       SQLite（operations + fencing 序列）
```

- **api**：命令入口与状态机（`queued → running → succeeded`，仅有这三态），发放有期限的租约与全局递增 fencing token，提交点做 fencing 校验。
- **executor**：领租约 → 带 fencing token 调用仪器端 → 提交回执；后台线程续租。崩溃后由编排层重启（compose `restart: unless-stopped`）。
- **instrument**：仪器模拟端，独立 SQLite 账本。同一 `operationId` 只真正应用一次（`appliedCount` 恒为 1），重复调用返回**同一** `receiptId`；低于已见最大 token 的请求判为 stale 拒绝（409）。

## 对外契约

| 请求 | 语义 |
|---|---|
| `POST /api/ramps` | body：`{operationId, magnetId, targetMilliAmps}`。新命令 → `201 queued`；同标识同参数重试 → `200` 返回原命令；同标识异参数 → `409`；字段错误 → `422`（`error.fields[]` 定位到具体字段） |
| `GET /api/ramps/{operationId}` | 查询状态；仅 `succeeded` 时携带 `receipt{receiptId, appliedCount}`。不存在 → `404` |
| `GET /healthz` | 存活探针（api 与 instrument 均提供，compose healthcheck 使用） |

失败响应（4xx）永不携带成功回执；`queued`/`running` 状态的响应也不含 `receipt` 字段。

## 崩溃收敛（verify 的 crash drill 验证的不变量）

执行器在「仪器已应用、状态未提交」之间崩溃后：

1. 租约到期，新执行器以**更高 fencing token** 重新领取该操作；
2. 仪器端按 `operationId` 幂等，返回**同一 receiptId**，`appliedCount` 保持 1；
3. 提交后操作收敛为 `succeeded`；旧 token 的迟到 apply/commit 均被 409 拒绝，无法覆盖结果。

## 运行

```bash
# Docker / Compose（宿主端口可配置）
API_HOST_PORT=8080 INSTRUMENT_HOST_PORT=9100 docker compose -f compose.yaml up --build

# 本地裸跑（零依赖，仅需 Python 3.11+）
API_PORT=8080 python -m app.api &
INSTRUMENT_PORT=9100 python -m app.instrument &
EXECUTOR_API_URL=http://127.0.0.1:8080 INSTRUMENT_URL=http://127.0.0.1:9100 python -m app.executor &

curl -X POST localhost:8080/api/ramps -d '{"operationId":"op-1","magnetId":"magnet-1","targetMilliAmps":1200}'
curl localhost:8080/api/ramps/op-1
```

崩溃演练：设 `CRASH_AFTER_APPLY=op-1` 启动执行器，它会在应用后、提交前 `os._exit(1)`；重启的执行器会按上述不变量收敛。

## 验证

```bash
python verify.py   # 或 python3 verify.py
```

单次执行四个阶段，按退出码报告（0 = 全过）：

1. **代码测试** — `tests/` 下 12 个用例（幂等重放、409/422 契约、回执可见性、fencing、崩溃收敛、迟到执行器）；
2. **构建检查** — 全模块字节码编译；有 Docker 时附加镜像构建与 `compose config` 校验；
3. **HTTP 冒烟** — 拉起真实 api/instrument/executor 进程，走完整公共契约；
4. **崩溃演练** — 注入 `CRASH_AFTER_APPLY`，确认最终 `succeeded`、`receiptId` 不变、`appliedCount == 1`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_HOST` / `API_PORT` / `API_DB` | `0.0.0.0` / `8080` / `/tmp/magnet-api.db` | API 监听与账本 |
| `INSTRUMENT_HOST` / `INSTRUMENT_PORT` / `INSTRUMENT_DB` | `0.0.0.0` / `9100` / `/tmp/magnet-instrument.db` | 仪器端监听与账本 |
| `INSTRUMENT_URL` | `http://127.0.0.1:9100` | 执行器访问仪器端的 URL |
| `EXECUTOR_API_URL` / `EXECUTOR_ID` | `http://127.0.0.1:8080` / `executor-<pid>` | 执行器访问 API |
| `LEASE_SECONDS` | `5` | 租约期限 |
| `CLAIM_WAIT_SECONDS` / `RENEW_INTERVAL_SECONDS` | `2` / `1` | 领取轮询间隔 / 续租间隔 |
| `CRASH_AFTER_APPLY` | 空 | 逗号分隔的 operationId 列表，命中即在应用后崩溃（故障注入） |
| `API_HOST_PORT` / `INSTRUMENT_HOST_PORT` | `8080` / `9100` | compose 宿主端口映射 |
