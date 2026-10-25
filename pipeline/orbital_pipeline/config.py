"""Runtime settings, read from environment variables.

Nothing here has a secret default: in Kubernetes the values come from a ConfigMap and a Secret
(see k8s/pipeline-config.yaml). The database connection uses the standard libpq variables
(PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD), so it is not repeated here.
"""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    api_client_id: str
    api_client_secret: str
    commitments_dir: Path

    # Usage data starts on this date (from the vendor documentation).
    usage_start_date: date = date(2026, 1, 1)

    # Every incremental run re-reads this many days before the latest usage date it already
    # has. The vendor restates up to 30 days after the usage date, so 30 is the minimum that is
    # correct; the extra days are a cheap safety margin.
    lookback_days: int = 35

    # How long a run waits for another run to finish before giving up.
    lock_wait_seconds: int = 900

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_base_url=_required("BILLING_API_URL").rstrip("/"),
            api_client_id=_required("BILLING_API_CLIENT_ID"),
            api_client_secret=_required("BILLING_API_CLIENT_SECRET"),
            commitments_dir=Path(_required("COMMITMENTS_DIR")),
            usage_start_date=date.fromisoformat(os.environ.get("USAGE_START_DATE", "2026-01-01")),
            lookback_days=int(os.environ.get("USAGE_LOOKBACK_DAYS", "35")),
            lock_wait_seconds=int(os.environ.get("LOCK_WAIT_SECONDS", "900")),
        )
