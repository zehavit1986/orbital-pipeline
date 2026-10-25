"""Entry point: `python -m orbital_pipeline`. One run = ingest usage, ingest commitments, publish.

Safe to run at any time, as often as you like:
- only one run works at a time (advisory lock); a second run waits, then repeats the work;
- every step is idempotent, so repeating it changes nothing;
- every write is transactional, so a run killed at any point leaves consistent data behind,
  and the next run picks up from there.
"""

import logging
import signal
import sys

from . import db
from .billing_api import BillingApiClient
from .config import Settings
from .ingest_commitments import load_commitments
from .ingest_usage import load_usage
from .model import build_and_publish

log = logging.getLogger("orbital_pipeline")


def run() -> None:
    settings = Settings.from_env()
    client = BillingApiClient(settings.api_base_url, settings.api_client_id, settings.api_client_secret)

    with db.connect() as conn, db.pipeline_lock(conn, settings.lock_wait_seconds):
        db.ensure_schema(conn)
        run_id = db.start_run(conn)
        log.info("Pipeline run %d started", run_id)
        details = {}
        try:
            details["usage"] = load_usage(conn, client, settings.usage_start_date, settings.lookback_days)
            details["commitments"] = load_commitments(conn, settings.commitments_dir)
            details["model"] = build_and_publish(conn)
        except BaseException as exc:  # includes SystemExit from SIGTERM
            details["error"] = repr(exc)
            db.finish_run(conn, run_id, "failed", details)
            raise
        db.finish_run(conn, run_id, "succeeded", details)
        log.info("Pipeline run %d succeeded", run_id)


def _exit_on_sigterm(signum, frame):
    raise SystemExit(f"Received signal {signum}, stopping")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # Kubernetes sends SIGTERM before it kills a pod. As PID 1 in the container, Python would
    # ignore it; turning it into an exception rolls back the open transaction and releases the
    # lock straight away, instead of waiting for the SIGKILL. (A SIGKILL is fine too: Postgres
    # then rolls back and releases the lock when the connection drops.)
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        run()
    except Exception:
        log.exception("Pipeline run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
