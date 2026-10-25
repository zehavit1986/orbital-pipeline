"""Replay the reviewers' scenario from the assignment end to end and check the results.

    python scripts/grading_scenario.py

1. fresh cluster at the manifest's AS_OF; initial load, deleting the pipeline pod partway
2. AS_OF=2026-09-28; incremental run; export
3. another run; export; must equal step 2
4. two runs at the same time; export; must equal step 2
5. down + up, AS_OF=2026-09-28, one run; export; must equal step 2

Takes a few minutes. Exports go to out/step<N>/. Standard library only.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
NS = ["--context", "kind-orbital", "-n", "orbital"]
TABLES = ["contract_utilization_daily.csv", "unattributed_usage_daily.csv"]


def sh(*args, capture=False, check=True):
    print("+", " ".join(args), flush=True)
    result = subprocess.run(args, cwd=ROOT, check=check, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else result


def kubectl(*args, **kwargs):
    return sh("kubectl", *NS, *args, **kwargs)


def start_job(name):
    kubectl("delete", "job", name, "--ignore-not-found")
    kubectl("create", "job", name, "--from=cronjob/orbital-pipeline")


def wait_job(name):
    kubectl("wait", "--for=condition=complete", f"job/{name}", "--timeout=900s")


def set_as_of(day):
    kubectl("set", "env", "deployment/billing-api", f"AS_OF={day}")
    kubectl("rollout", "status", "deployment/billing-api", "--timeout=300s")


def export(step):
    sh(sys.executable, "run.py", "export", str(OUT / f"step{step}"))


def assert_same(step, reference=2):
    for table in TABLES:
        a = (OUT / f"step{reference}" / table).read_text().splitlines()
        b = (OUT / f"step{step}" / table).read_text().splitlines()
        if sorted(a) != sorted(b):
            sys.exit(f"FAIL: step {step} {table} differs from step {reference}")
    print(f"OK: step {step} matches step {reference}", flush=True)


def delete_pod_partway(job):
    """Wait until the run has loaded a few usage windows, then delete its pod."""
    deadline = time.time() + 300
    while time.time() < deadline:
        pod = kubectl("get", "pods", "-l", f"job-name={job}", "-o",
                      "jsonpath={.items[0].metadata.name}", capture=True, check=False)
        logs = kubectl("logs", pod, capture=True, check=False) if pod else ""
        if logs and logs.count("records fetched") >= 3:
            kubectl("delete", "pod", pod, "--wait=false")
            print(f"Deleted pod {pod} in the middle of the backfill", flush=True)
            return
        time.sleep(2)
    sys.exit("FAIL: the initial run never got going")


def main():
    sh(sys.executable, "run.py", "up")

    print("\n== 1. initial load, pod deleted partway", flush=True)
    start_job("run-1")
    delete_pod_partway("run-1")
    wait_job("run-1")

    print("\n== 2. incremental run at AS_OF=2026-09-28", flush=True)
    set_as_of("2026-09-28")
    start_job("run-2")
    wait_job("run-2")
    export(2)

    print("\n== 3. same again", flush=True)
    start_job("run-3")
    wait_job("run-3")
    export(3)
    assert_same(3)

    print("\n== 4. two runs at the same time", flush=True)
    start_job("run-4a")
    start_job("run-4b")
    wait_job("run-4a")
    wait_job("run-4b")
    export(4)
    assert_same(4)

    print("\n== 5. full reload on a fresh cluster", flush=True)
    sh(sys.executable, "run.py", "down")
    sh(sys.executable, "run.py", "up")
    set_as_of("2026-09-28")
    start_job("run-5")
    wait_job("run-5")
    export(5)
    assert_same(5)

    print("\nAll steps passed.")


if __name__ == "__main__":
    main()
