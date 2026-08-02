# EDA-Agent-Bench Adapter 测试方对接文档

## 1. 文档目的

本文档面向调用 OpenROAD Agent 进行 EDA-Agent-Bench 测试的评测方，描述任务准备、提交、
状态查询、事件获取、产物下载和取消协议。

Adapter 只负责运行 Agent 并返回可审计的轨迹和产物，不持有隐藏 oracle，也不判断 benchmark
是否通过。`succeeded` 仅表示 Agent 正常完成提交，最终分数应由评测方基于返回产物重新计算。

当前协议版本为 `1.0`。

## 2. 连接信息

默认服务地址：

```text
http://127.0.0.1:8010
```

服务没有 HTTP 鉴权，并且强制监听 loopback。远程评测方应通过 SSH 隧道连接：

```bash
ssh -N -L 8010:127.0.0.1:8010 <user>@<benchmark-host>
```

建立隧道后，评测方仍通过 `http://127.0.0.1:8010` 调用。

健康检查：

```bash
curl -sS http://127.0.0.1:8010/healthz
```

```json
{
  "status": "ok",
  "work_dir": "/absolute/path/to/openroad_benchmark_work"
}
```

查询 Agent 支持的 Level、工具和本机 EDA 可执行文件状态：

```bash
curl -sS http://127.0.0.1:8010/v1/benchmark/capabilities
```

评测方应在开始测试前检查 `executables.<name>.available`。如果题目依赖缺失的工具，应跳过该题或
要求运行方补充环境，不能把环境缺失计为 Agent 推理失败。

## 3. Snapshot 准备

### 3.1 目录要求

Adapter 与 snapshot 位于同一台机器，因此请求传递的是绝对目录路径，不上传文件。例如：

```text
/data/eda-agent-bench/snapshots/L3-SIGN-01-aes-01/
├── benchmark_manifest.json
├── rtl/
├── constraints/
├── platform/
├── checkpoints/
└── inputs/
```

要求：

- `snapshot_path` 必须是绝对路径并指向目录；
- snapshot 根目录和内部文件不能是符号链接；
- 必须包含 `benchmark_manifest.json`；
- manifest 中声明的文件必须存在且 SHA-256 完全一致；
- Level 4 mutation 和 Level 5 初始状态由评测方提前生成；
- snapshot 不得包含 hidden oracle、golden patch、mutation metadata 或参考 Pareto front。

### 3.2 benchmark_manifest.json

```json
{
  "schema_version": "1.0",
  "design": {
    "name": "aes",
    "top": "aes_cipher_top"
  },
  "platform": "nangate45",
  "starting_checkpoint": "post_route",
  "ffe": {
    "full_flow_seconds": 1800.0
  },
  "files": [
    {
      "path": "rtl/aes.v",
      "sha256": "<64-character lowercase SHA-256>",
      "role": "rtl",
      "immutable": true
    },
    {
      "path": "constraints/aes.sdc",
      "sha256": "<64-character lowercase SHA-256>",
      "role": "constraint",
      "immutable": true
    },
    {
      "path": "checkpoints/aes_post_route.odb",
      "sha256": "<64-character lowercase SHA-256>",
      "role": "checkpoint",
      "immutable": false
    }
  ]
}
```

`ffe.full_flow_seconds` 用于计算：

```text
FFE = 累计 EDA 工具运行秒数 / full_flow_seconds
```

生成文件 SHA-256：

```bash
sha256sum /data/eda-agent-bench/snapshots/L3-SIGN-01-aes-01/rtl/aes.v
```

## 4. 提交任务

### 4.1 接口

```http
POST /v1/benchmark/runs
Content-Type: application/json
```

完整示例：

```json
{
  "schema_version": "1.0",
  "request_id": "eval-2026-L3-SIGN-01-aes-01-seed-1",
  "snapshot_path": "/data/eda-agent-bench/snapshots/L3-SIGN-01-aes-01",
  "task": {
    "task_id": "L3-SIGN-01-aes-01",
    "level": 3,
    "domain": "physical_signoff",
    "prompt": "Starting from the routed AES design, generate GDS and complete DRC and LVS verification. Return a consolidated signoff report.",
    "tool_catalog": [
      "run_klayout_gds",
      "run_drc",
      "run_lvs"
    ],
    "budget": {
      "timeout_seconds": 1800,
      "max_primary_tool_calls": 5,
      "max_total_tool_calls": 8,
      "max_llm_turns": 8,
      "max_parallelism": 2,
      "max_failed_runs": 2,
      "cpu_core_hours": 1.0,
      "full_flow_equivalents": 2.0
    },
    "immutable": [
      "rtl/**",
      "constraints/**"
    ],
    "forbidden_operations": [
      "modify_rtl",
      "relax_clock"
    ],
    "required_outputs": [
      "*.gds",
      "*drc_report*",
      "*lvs_report*"
    ],
    "level_contract": {
      "required_capabilities": [
        "run_klayout_gds",
        "run_drc",
        "run_lvs"
      ],
      "precedence": [
        "run_klayout_gds before run_drc",
        "run_klayout_gds before run_lvs"
      ]
    }
  }
}
```

字段约束：

| 字段 | 要求 |
|---|---|
| `schema_version` | 固定为 `1.0` |
| `request_id` | 1–200 字符；幂等键，同一 ID 重复提交返回原 run |
| `snapshot_path` | 评测主机上的绝对目录路径 |
| `task.task_id` | 1–200 字符 |
| `task.level` | 整数 `1`–`5` |
| `task.domain` | 非空领域名称 |
| `task.prompt` | 给 Agent 的自然语言任务 |
| `task.tool_catalog` | Agent 实际可见的 EDA 工具；建议总是显式传递 |
| `task.immutable` | 相对于 snapshot 根目录的 glob |
| `task.required_outputs` | 对最终 run 产物相对路径匹配的 glob |
| `task.level_contract` | 公开的机器可检查工作流约束 |

预算字段省略时的默认值：

| 字段 | 默认值/范围 |
|---|---|
| `timeout_seconds` | `3600`；范围 1 秒至 7 天 |
| `max_parallelism` | `1`；范围 1–256 |
| 其他预算字段 | `null`，表示不设置该项上限 |

`level_contract` 当前自动检查：

- `required_capabilities`：要求对应工具实际被调用；
- `precedence` 或 `precedence_constraints`：支持 `"A before B"`、`["A", "B"]` 或
  `{"before":"A","after":"B"}`；
- Level 1 默认执行 atomic 检查；设置 `"atomic": false` 可关闭。

### 4.2 提交命令

```bash
curl -sS -X POST http://127.0.0.1:8010/v1/benchmark/runs \
  -H 'Content-Type: application/json' \
  --data @task.json
```

成功返回 HTTP `202`：

```json
{
  "run_id": "run_0123456789abcdef0123456789abcdef",
  "status": "queued",
  "created": true,
  "status_url": "/v1/benchmark/runs/run_0123456789abcdef0123456789abcdef"
}
```

若使用相同 `request_id` 重复提交，返回相同 `run_id` 且 `created=false`。

## 5. 查询状态与结果

```bash
RUN_ID=run_0123456789abcdef0123456789abcdef
curl -sS "http://127.0.0.1:8010/v1/benchmark/runs/${RUN_ID}"
```

状态转换：

```text
queued → preparing → running → succeeded
                           ├→ failed
                           ├→ budget_exceeded
                           └→ cancelling → cancelled
```

终态：

- `succeeded`
- `failed`
- `cancelled`
- `budget_exceeded`

终态响应的 `result` 示例：

```json
{
  "schema_version": "1.0",
  "run_id": "run_...",
  "task_id": "L3-SIGN-01-aes-01",
  "status": "succeeded",
  "answer": "Agent final response",
  "submission": {
    "conclusion": {},
    "metrics": {},
    "effective_parameters": {},
    "artifacts": [],
    "artifact_manifest": "artifact_manifest.json"
  },
  "usage": {
    "wall_time_seconds": 123.4,
    "cpu_core_hours": 0.02,
    "tool_calls": 4,
    "primary_tool_calls": 3,
    "failed_tool_calls": 0,
    "llm_calls": 4,
    "input_tokens": 1000,
    "output_tokens": 300,
    "full_flow_equivalents": 0.31
  },
  "violations": [],
  "warnings": [],
  "error": ""
}
```

评测方必须使用 `result.status`、`result.violations`、事件和实际产物，而不是仅解析 `answer`。

## 6. 实时事件

接口使用 Server-Sent Events：

```bash
curl -N "http://127.0.0.1:8010/v1/benchmark/runs/${RUN_ID}/events"
```

事件类型包括：

- `run_started`
- `llm_turn`
- `tool_started`
- `tool_finished`
- `remote_job`
- `run_error`
- `run_finished`
- `terminal`

工具完成事件示例：

```json
{
  "sequence": 7,
  "event_id": "run_...:7",
  "run_id": "run_...",
  "type": "tool_finished",
  "tool": "report_timing",
  "parent_event_id": "run_...:6",
  "status": "success",
  "effective_parameters": {},
  "output_artifacts": [],
  "metrics": {},
  "result": {},
  "error": "",
  "timestamp": "2026-08-02T10:00:00Z"
}
```

以 `: heartbeat` 开头的 SSE 注释是保活消息，应忽略。收到 `terminal` 后连接正常结束。

## 7. 产物获取

列出产物：

```bash
curl -sS "http://127.0.0.1:8010/v1/benchmark/runs/${RUN_ID}/manifest"
```

也可使用等价接口：

```text
GET /v1/benchmark/runs/{run_id}/artifacts
```

响应：

```json
{
  "run_id": "run_...",
  "artifacts": [
    {
      "artifact_id": "sha256:...",
      "path": "workspace/sessions/.../routing/results/aes.gds",
      "size_bytes": 123456,
      "sha256": "...",
      "producer_event_id": "run_...:12",
      "consumers": []
    }
  ]
}
```

下载单个产物时，使用 manifest 返回的 `path`，并进行 URL 编码：

```bash
curl -fL \
  "http://127.0.0.1:8010/v1/benchmark/runs/${RUN_ID}/artifacts/workspace/sessions/.../aes.gds" \
  -o aes.gds
```

安全限制：原始 `input/`、session 的 `designs/`、`task.json` 和 immutable 初始哈希文件不可下载，
会返回 HTTP `403`。评测方已经持有 snapshot，不应通过 Adapter 重新获取输入。

## 8. 取消任务

```bash
curl -sS -X POST \
  "http://127.0.0.1:8010/v1/benchmark/runs/${RUN_ID}/cancel"
```

Adapter 会：

1. 将任务标为 `cancelling`；
2. 阻止新的工具调用；
3. 终止本地任务进程组；
4. 尝试停止已登记的 Ray jobs；
5. 最终标记为 `cancelled`。

如果 Ray job 无法确认停止，会在内部记录为 orphaned；评测方应保留对应 run 日志用于诊断。

## 9. 错误码

| HTTP 状态 | 含义 |
|---|---|
| `202` | 任务已创建或返回幂等的已有任务 |
| `403` | 请求下载输入或受保护文件 |
| `404` | run 或 artifact 不存在 |
| `422` | 请求 schema、snapshot、manifest 或 SHA-256 校验失败 |

运行期间的 EDA/Agent 错误不使用 HTTP 5xx 表示，而是在 run 终态的 `status` 和 `error` 中返回。

## 10. 推荐评测方调用流程

```text
检查 /healthz 和 /capabilities
        ↓
准备 snapshot 与 benchmark_manifest.json
        ↓
POST /v1/benchmark/runs
        ↓
保存 run_id
        ↓
轮询 GET /runs/{run_id} 或订阅 /events
        ↓
等待终态
        ↓
下载 manifest 中的实际产物
        ↓
在评测方隔离环境执行 hidden signoff/evaluator
        ↓
将 correctness、QoR 与 Adapter usage/trace 合并计分
```

评测方不应：

- 将 `succeeded` 直接视为 TaskPass；
- 信任 Agent 自述的 metrics 而不重新验证；
- 在 snapshot 中放入 hidden evaluator 或 golden 数据；
- 为不同题目复用同一个 `request_id`；
- 将 API 直接暴露到公网。

## 11. 最小轮询脚本

以下脚本依赖 `curl` 和 `jq`：

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8010}"
TASK_FILE="${1:?usage: run_case.sh task.json}"

RESPONSE="$(curl -fsS -X POST "${BASE_URL}/v1/benchmark/runs" \
  -H 'Content-Type: application/json' \
  --data "@${TASK_FILE}")"
RUN_ID="$(jq -r '.run_id' <<<"${RESPONSE}")"
echo "run_id=${RUN_ID}"

while true; do
  RUN="$(curl -fsS "${BASE_URL}/v1/benchmark/runs/${RUN_ID}")"
  STATUS="$(jq -r '.status' <<<"${RUN}")"
  echo "status=${STATUS}"
  case "${STATUS}" in
    succeeded|failed|cancelled|budget_exceeded)
      jq . <<<"${RUN}"
      break
      ;;
  esac
  sleep 2
done

curl -fsS "${BASE_URL}/v1/benchmark/runs/${RUN_ID}/manifest" \
  -o "${RUN_ID}_manifest.json"
```

## 12. 服务端启动信息

由运行方在项目根目录启动两个进程：

```bash
pip install -e .
openroad-benchmark-api
openroad-benchmark-worker
```

API 只接收和查询任务；没有 Worker 时任务会一直停留在 `queued`。
