"""Start the demo in one command: Neo4j with the real data, GDS scores, predictions, API and interface. Owner: D.

Run with `python -m scripts.start_demo` (Windows: `.venv\\Scripts\\python -m scripts.start_demo`).

1. Neo4j 5 + GDS in Docker, container `scout-neo4j-demo` on port 7690, with a named volume: the graph survives a
   restart, so only the first start loads the data (about 3 minutes). It never touches the test database (7688).
2. The Transfermarkt DuckDB file is downloaded if `data/` does not have it (same URL as `make data`).
3. Empty graph: ingestion of A (`scripts.seed_data`). No predictions yet: training of C
   (`ml.train_link_prediction`). Every start: GDS refresh (a few seconds; the scores are recomputed).
4. The API starts on the first free port from 8000 and the browser opens `/ui`. Ctrl+C stops the API;
   `--stop` also stops Neo4j.

`--mini` serves the fictitious mini graph of the tests instead (container `scout-neo4j-mini`, port 7691, reloaded at
every start): no download, ready in about 30 seconds. The LLM settings come from `.env` as usual; `API_KEY` too,
unless `--api-key` is given.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUCKDB_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/transfermarkt-datasets.duckdb"
NEO4J_IMAGE = "neo4j:5"
START_TIMEOUT_S = 180
API_TIMEOUT_S = 60


@dataclass(frozen=True)
class Target:
    """One Neo4j container the demo can run on."""

    container: str
    port: int
    password: str
    volume: str | None


REAL = Target("scout-neo4j-demo", 7690, "demopassword", "scout-neo4j-demo-data")
MINI = Target("scout-neo4j-mini", 7691, "minipassword", None)


def log(message: str) -> None:
    """Print one progress line."""
    print(f"[demo] {message}", flush=True)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker command and return its result."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def docker_run_args(target: Target) -> list[str]:
    """Arguments of `docker run` for a target (GDS enabled, heap sized for the real graph)."""
    args = ["run", "-d", "--name", target.container, "-p", f"{target.port}:7687"]
    if target.volume:
        args += ["-v", f"{target.volume}:/data"]
    else:
        args.append("--rm")
    return args + [
        "-e",
        f"NEO4J_AUTH=neo4j/{target.password}",
        "-e",
        'NEO4J_PLUGINS=["graph-data-science"]',
        "-e",
        "NEO4J_dbms_security_procedures_unrestricted=gds.*",
        "-e",
        "NEO4J_server_memory_heap_max__size=2G",
        "-e",
        "NEO4J_server_memory_pagecache_size=1G",
        NEO4J_IMAGE,
    ]


def container_state(name: str) -> str:
    """`running`, `exited`, ... or `missing` when the container does not exist."""
    result = docker("inspect", "-f", "{{.State.Status}}", name, check=False)
    return result.stdout.strip() if result.returncode == 0 else "missing"


def ensure_container(target: Target) -> None:
    """Start the container of the target, creating it if needed."""
    state = container_state(target.container)
    if state == "running":
        log(f"{target.container} is already running (bolt://localhost:{target.port})")
    elif state == "missing":
        log(f"creating {target.container} on port {target.port}")
        docker(*docker_run_args(target))
    else:
        log(f"restarting {target.container}")
        docker("start", target.container)


def target_env(target: Target, api_key: str | None) -> dict[str, str]:
    """Environment of the child processes: it overrides `.env` for Neo4j only (and the key if given)."""
    env = dict(os.environ)
    env.update(NEO4J_URI=f"bolt://localhost:{target.port}", NEO4J_USER="neo4j", NEO4J_PASSWORD=target.password)
    if api_key:
        env["API_KEY"] = api_key
    return env


def connect(target: Target):
    """Client on the target, once it answers (Neo4j takes 20 to 60 s to start)."""
    from app.database.neo4j import Neo4jClient  # pylint: disable=import-outside-toplevel

    deadline = time.monotonic() + START_TIMEOUT_S
    while True:
        client = Neo4jClient(f"bolt://localhost:{target.port}", ("neo4j", target.password))
        if client.ping():
            return client
        client.close()
        if time.monotonic() > deadline:
            raise SystemExit(f"Neo4j did not answer on port {target.port} within {START_TIMEOUT_S} s")
        time.sleep(2)


def count(client, query: str) -> int:
    """First column of the first row of a counting query."""
    rows = client.read(query)
    return int(rows[0]["n"]) if rows else 0


def run_module(module: str, env: dict[str, str], *args: str) -> None:
    """Run `python -m <module>` with the demo environment; stop the demo if it fails."""
    log(f"running {module} {' '.join(args)}".rstrip())
    if subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT, env=env, check=False).returncode:
        raise SystemExit(f"{module} failed: see the output above")


def download_duckdb(path: Path) -> None:
    """Download the Transfermarkt DuckDB file if it is missing."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading the Transfermarkt DuckDB file to {path} (about 200 MB)")
    partial = path.with_suffix(".part")
    with urllib.request.urlopen(DUCKDB_URL, timeout=60) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out, length=1 << 20)
    partial.replace(path)


def prepare_real(client, env: dict[str, str]) -> None:
    """Load the real graph and the predictions when they are missing."""
    if count(client, "MATCH (p:Player) RETURN count(p) AS n") == 0:
        download_duckdb(ROOT / env.get("DUCKDB_PATH", "data/transfermarkt-datasets.duckdb"))
        log("empty graph: loading the real data (about 3 minutes, only the first time)")
        run_module("scripts.seed_data", env)
    if count(client, "MATCH ()-[r:PREDICTED_LINK]->() RETURN count(r) AS n") == 0:
        log("no predictions yet: training the link-prediction model (about 30 s)")
        run_module("ml.train_link_prediction", env)


def prepare_mini(client) -> None:
    """Reload the mini graph of the tests (constraints, then every fixture file)."""
    from scripts.cypher_files import run_file  # pylint: disable=import-outside-toplevel

    fixtures = ROOT / "tests" / "fixtures"
    log("loading the mini graph of the tests")
    client.write("MATCH (n) DETACH DELETE n")
    run_file(client, ROOT / "scripts" / "constraints.cypher")
    run_file(client, fixtures / "mini_graph.cypher")
    for extra in sorted(fixtures.glob("*.cypher")):
        if extra.name != "mini_graph.cypher":
            run_file(client, extra)


def refresh_gds(client) -> None:
    """Recompute the network scores (PageRank, Louvain, betweenness, FastRP)."""
    from app.services.gds import GdsService  # pylint: disable=import-outside-toplevel

    report = GdsService(client).refresh()
    log(f"GDS scores computed in {report.duration_s:.1f} s")


def free_port(start: int, attempts: int = 20) -> int:
    """First port from `start` that nothing listens on."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit(f"no free port between {start} and {start + attempts - 1}")


def wait_for_api(url: str, process: subprocess.Popen) -> None:
    """Wait until `/api/health` answers, or stop if the API process died."""
    deadline = time.monotonic() + API_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("the API stopped while starting: see the output above")
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"the API did not answer on {url} within {API_TIMEOUT_S} s")


def serve(env: dict[str, str], port: int, open_browser: bool) -> None:
    """Run the API until Ctrl+C, opening the interface once it answers."""
    url = f"http://localhost:{port}"
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    process = subprocess.Popen(command, cwd=ROOT, env=env)  # pylint: disable=consider-using-with
    try:
        wait_for_api(url, process)
        log(f"interface: {url}/ui   ·   Swagger: {url}/docs   ·   Ctrl+C to stop the API")
        if open_browser:
            webbrowser.open(f"{url}/ui")
        process.wait()
    except KeyboardInterrupt:
        log("stopping the API")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description="Start the demo: Neo4j with the real data, GDS, predictions, API, UI.")
    parser.add_argument("--mini", action="store_true", help="serve the fictitious mini graph of the tests")
    parser.add_argument("--port", type=int, default=8000, help="first API port to try (default 8000)")
    parser.add_argument("--api-key", help="API key of the protected routes (default: API_KEY from .env)")
    parser.add_argument(
        "--import-rumours", action="store_true", help="import press rumours first (spends a little LLM credit)"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--stop", action="store_true", help="stop the demo Neo4j containers and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point of the demo."""
    args = parse_args(argv)
    if shutil.which("docker") is None or docker("info", check=False).returncode:
        raise SystemExit("Docker is not running: start Docker Desktop, then run this command again")
    if args.stop:
        for target in (REAL, MINI):
            if container_state(target.container) == "running":
                docker("stop", target.container)
                log(f"{target.container} stopped")
        return
    target = MINI if args.mini else REAL
    env = target_env(target, args.api_key)
    ensure_container(target)
    client = connect(target)
    try:
        if args.mini:
            prepare_mini(client)
        else:
            prepare_real(client, env)
            if args.import_rumours:
                run_module("scripts.import_articles", env)
        refresh_gds(client)
        players = count(client, "MATCH (p:Player) RETURN count(p) AS n")
        log(f"graph ready: {players:,} players")
    finally:
        client.close()
    serve(env, free_port(args.port), not args.no_browser)


if __name__ == "__main__":
    main()
