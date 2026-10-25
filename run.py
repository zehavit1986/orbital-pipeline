"""Run the Orbital pipeline on a local kind cluster.

    python run.py up               create the cluster (if needed), build + load images, deploy
    python run.py export OUT_DIR   write the two model tables as CSV files
    python run.py down             delete the cluster

Standard library only, no shell features, so it works the same on macOS, Linux and Windows.
Needs docker, kind and kubectl on the PATH.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLUSTER = "orbital"
CONTEXT = f"kind-{CLUSTER}"
NAMESPACE = "orbital"

# image tag -> build context (relative to ROOT)
IMAGES = {
    "orbital-billing-api:1.0": "mock_api",
    "orbital-pipeline:1.0": "pipeline",
}

EXPORTS = {
    "contract_utilization_daily": """
        SELECT contract_id, customer_id, date, commitment_usd, term_days, days_elapsed,
               consumed_usd, consumed_to_date_usd, expected_to_date_usd, utilization_pct,
               projected_term_usd, at_risk::text AS at_risk
        FROM contract_utilization_daily
        ORDER BY contract_id, date""",
    "unattributed_usage_daily": """
        SELECT customer_id, date, amount_usd
        FROM unattributed_usage_daily
        ORDER BY customer_id, date""",
}


# -- helpers -------------------------------------------------------------------------------

def run(*args: str, capture: bool = False, stdin: bytes | None = None, check: bool = True):
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, cwd=ROOT, input=stdin, check=check,
                          stdout=subprocess.PIPE if capture else None)


def kubectl(*args: str, **kwargs):
    return run("kubectl", "--context", CONTEXT, *args, **kwargs)


def require_tools(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        sys.exit(f"Please install {', '.join(missing)} and make sure it is on your PATH.")


def cluster_exists() -> bool:
    clusters = run("kind", "get", "clusters", capture=True).stdout.decode().split()
    return CLUSTER in clusters


def load_image_into_kind(image: str) -> None:
    """Copy a local image into the kind node.

    `kind load docker-image` fails on some Docker Desktop setups (containerd image store);
    loading a saved archive works there, so fall back to that.
    """
    if run("kind", "load", "docker-image", image, "--name", CLUSTER, check=False).returncode == 0:
        return
    print(f"Retrying {image} via an image archive", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = str(Path(tmp) / "image.tar")
        run("docker", "save", "-o", archive, image)
        run("kind", "load", "image-archive", archive, "--name", CLUSTER)


# -- commands ------------------------------------------------------------------------------

def up() -> None:
    require_tools("docker", "kind", "kubectl")

    if cluster_exists():
        print(f"kind cluster '{CLUSTER}' already exists")
    else:
        run("kind", "create", "cluster", "--name", CLUSTER, "--wait", "180s")

    for image, context in IMAGES.items():
        run("docker", "build", "-t", image, context)
        load_image_into_kind(image)

    # The billing API exactly as provided (it also creates the namespace).
    kubectl("apply", "-f", "mock_api/k8s/billing-api.yaml")

    # The Finance workbooks as a ConfigMap, so new snapshots don't need a new image.
    # `create --dry-run | apply` makes this an idempotent create-or-update.
    configmap = kubectl("create", "configmap", "orbital-commitments", "-n", NAMESPACE,
                        "--from-file=data/commitments", "--dry-run=client", "-o", "yaml",
                        capture=True).stdout
    kubectl("apply", "-f", "-", stdin=configmap)

    # Our config, Postgres and the CronJob.
    kubectl("apply", "-f", "k8s/")

    kubectl("-n", NAMESPACE, "rollout", "status", "deployment/billing-api", "--timeout=300s")
    kubectl("-n", NAMESPACE, "rollout", "status", "statefulset/postgres", "--timeout=300s")
    print("\nReady. Start a run with:\n"
          f"  kubectl -n {NAMESPACE} create job run-1 --from=cronjob/orbital-pipeline")


def export(out_dir: str) -> None:
    require_tools("kubectl")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for table, query in EXPORTS.items():
        copy = f"COPY ({' '.join(query.split())}) TO STDOUT WITH (FORMAT csv, HEADER)"
        result = kubectl("-n", NAMESPACE, "exec", "postgres-0", "-c", "postgres", "--",
                         "psql", "-U", "orbital", "-d", "orbital", "-v", "ON_ERROR_STOP=1",
                         "-c", copy, capture=True)
        path = out / f"{table}.csv"
        path.write_bytes(result.stdout)
        rows = len(result.stdout.splitlines()) - 1  # minus the header
        print(f"Wrote {path} ({rows} rows)")


def down() -> None:
    require_tools("kind")
    run("kind", "delete", "cluster", "--name", CLUSTER)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="create the cluster and deploy everything")
    export_parser = commands.add_parser("export", help="write the model tables as CSV")
    export_parser.add_argument("out_dir")
    commands.add_parser("down", help="delete the cluster")
    args = parser.parse_args()

    try:
        if args.command == "up":
            up()
        elif args.command == "export":
            export(args.out_dir)
        elif args.command == "down":
            down()
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}")


if __name__ == "__main__":
    main()
