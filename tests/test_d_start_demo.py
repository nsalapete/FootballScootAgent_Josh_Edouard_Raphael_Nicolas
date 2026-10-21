"""Tests of the one-command demo launcher, without Docker, Neo4j or network. Owner: D."""

import socket

import pytest

from scripts import start_demo
from scripts.start_demo import MINI, REAL, docker_run_args, free_port, parse_args, target_env


def test_real_target_keeps_its_data_in_a_volume_and_never_uses_the_test_ports():
    args = docker_run_args(REAL)
    assert ["-v", "scout-neo4j-demo-data:/data"] == args[args.index("-v"): args.index("-v") + 2]
    assert "--rm" not in args and "7690:7687" in args
    assert 'NEO4J_PLUGINS=["graph-data-science"]' in args
    assert {REAL.port, MINI.port}.isdisjoint({7687, 7688, 7689})


def test_mini_target_is_disposable():
    args = docker_run_args(MINI)
    assert "--rm" in args and "-v" not in args and "7691:7687" in args


def test_environment_overrides_neo4j_only_unless_a_key_is_given(monkeypatch):
    monkeypatch.setenv("API_KEY", "from-env")
    env = target_env(REAL, None)
    assert env["NEO4J_URI"] == "bolt://localhost:7690" and env["NEO4J_PASSWORD"] == "demopassword"
    assert env["API_KEY"] == "from-env"
    assert target_env(MINI, "demo")["API_KEY"] == "demo"


def test_free_port_skips_a_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert free_port(port, attempts=5) != port


def test_options():
    args = parse_args(["--mini", "--port", "8100", "--no-browser"])
    assert args.mini and args.port == 8100 and args.no_browser and not args.import_rumours and not args.stop
    assert parse_args([]).port == 8000


def test_a_clear_message_when_docker_is_missing(monkeypatch):
    monkeypatch.setattr(start_demo.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="Docker is not running"):
        start_demo.main(["--no-browser"])
