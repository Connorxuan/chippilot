"""Console entry points for the adapter API and worker."""

from __future__ import annotations

import argparse

from benchmark_adapter.config import AdapterConfig


def api_main() -> None:
    import uvicorn
    from benchmark_adapter.api import create_app

    cfg = AdapterConfig()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, reload=False)


def worker_main() -> None:
    from benchmark_adapter.worker import Worker

    parser = argparse.ArgumentParser(prog="openroad-benchmark-worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    Worker(AdapterConfig()).run(once=args.once)
