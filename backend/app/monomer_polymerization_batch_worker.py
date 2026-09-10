from __future__ import annotations

import argparse
import logging
import signal

from app.config import Settings
from app.services.polymerization_batch.models import BatchSettings
from app.services.polymerization_batch.worker import BatchWorker


def main():
    parser = argparse.ArgumentParser(description="Durable CPU-only SMiPoly batch worker")
    parser.add_argument("--healthcheck", action="store_true")
    arguments = parser.parse_args()
    worker = BatchWorker(Settings().app_postgres_dsn, BatchSettings.from_env())
    if arguments.healthcheck:
        if not worker.config.enabled:
            raise SystemExit(0)
        from app.services.deployment_control import get_drain_state
        with worker.repository.connection() as conn:
            if get_drain_state(conn).enabled:
                raise SystemExit(0)
        status = worker.repository.worker_status()
        raise SystemExit(0 if status and status["fresh"] and (status["available"] or not worker.config.enabled) else 1)
    logging.basicConfig(level=logging.INFO)
    def stop(signum, frame):
        worker.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()


if __name__ == "__main__":
    main()
