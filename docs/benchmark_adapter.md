# EDA-Agent-Bench Adapter

The adapter exposes the OpenROAD agent through a persistent, auditable local API. It is
independent of `experiment/` and does not perform hidden benchmark evaluation.

## Start

```bash
pip install -e .
openroad-benchmark-api
openroad-benchmark-worker
```

The API binds to `127.0.0.1:8010` by default. To share it without exposing an unauthenticated
service, use SSH forwarding:

```bash
ssh -L 8010:127.0.0.1:8010 benchmark-host
```

Configuration variables:

- `OPENROAD_BENCHMARK_WORK_DIR` (default `./openroad_benchmark_work`)
- `OPENROAD_BENCHMARK_HOST` (default `127.0.0.1`)
- `OPENROAD_BENCHMARK_PORT` (default `8010`)
- `OPENROAD_BENCHMARK_POLL_SECONDS` (default `1`)
- `OPENROAD_BENCHMARK_CANCEL_GRACE_SECONDS` (default `10`)

## Snapshot contract

`snapshot_path` must be an absolute directory without symbolic links. It must contain
`benchmark_manifest.json`, whose `files` entries provide a relative path, SHA-256, role, and
immutable flag. Level 4 mutations and Level 5 starting states are prepared by the evaluator;
golden data must never be included in the snapshot.

## Example

```bash
curl -X POST http://127.0.0.1:8010/v1/benchmark/runs \
  -H 'Content-Type: application/json' \
  --data @task.json
```

Poll the returned status URL or consume `/events`. A `succeeded` run only means that the agent
submitted a result. The evaluator remains responsible for hidden signoff and scoring.

The complete test-party handoff document is available in
[`benchmark_test_party_integration.md`](benchmark_test_party_integration.md).
