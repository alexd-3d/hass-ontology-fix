"""Static and process-unit checks for the multi-process Memgraph add-on."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ADDON = ROOT / "memgraph_addon"


def test_addon_does_not_publish_graphql_or_lab_ports() -> None:
    config = yaml.safe_load((ADDON / "config.yaml").read_text())
    assert "4000/tcp" not in config.get("ports", {})
    assert "3000/tcp" not in config.get("ports", {})
    assert set(config["arch"]) == {"amd64", "aarch64"}
    assert config["ingress"] is True
    assert config["ingress_port"] == 3000


def test_supervisor_models_independent_process_health_and_signals() -> None:
    supervisor = (ADDON / "supervisor.conf").read_text()
    run_script = (ADDON / "run.sh").read_text()
    healthcheck = (ADDON / "healthcheck.sh").read_text()

    assert "[program:memgraph]" in supervisor
    assert "[program:graphql]" in supervisor
    assert "[program:lab]" in supervisor
    lab_section = supervisor.split("[program:lab]", 1)[1]
    assert "autostart=false" in lab_section
    assert "autorestart=false" in lab_section
    assert "stopsignal=TERM" in supervisor
    assert "stopasgroup=true" in supervisor
    assert "chmod 600" in run_script
    assert "graphql_url" in run_script
    assert "graphql_token" in run_script
    assert "memgraph" in healthcheck and "graphql" in healthcheck and "lab" in healthcheck


def test_dockerfile_pins_images_by_digest_and_packages_exact_node_lock() -> None:
    dockerfile = (ADDON / "Dockerfile").read_text()
    notices = (ADDON / "THIRD_PARTY_NOTICES.md").read_text()
    assert "memgraph/memgraph:3.12.0@sha256:" in dockerfile
    assert "memgraph/lab" in dockerfile and "@sha256:" in dockerfile
    assert re.search(r"node:\d+\.\d+\.\d+-bookworm-slim@sha256:[0-9a-f]{64}", dockerfile)
    assert "npm ci --omit=dev" in dockerfile
    assert "graphql/package-lock.json" in dockerfile
    assert "linux/amd64 and linux/arm64" in notices
    assert "Community License" in notices
    assert "GraphQL.js `16.11.0` (MIT)" in notices
    assert "Apollo Server `5.5.1` (MIT)" in notices
    assert "Neo4j JavaScript Driver `6.2.0` (Apache-2.0)" in notices


def _memgraph_command() -> list[str]:
    supervisor = (ADDON / "supervisor.conf").read_text()
    section = supervisor.split("[program:memgraph]", 1)[1].split("\n[program:", 1)[0]
    line = next(line for line in section.splitlines() if line.startswith("command="))
    return line.removeprefix("command=").split()


def test_memgraph_runtime_flags_are_explicit() -> None:
    """ON-039: telemetry off, hourly snapshots, no-op deltas off, slow-query log."""
    flags = _memgraph_command()
    assert "--telemetry-enabled=false" in flags
    assert "--storage-snapshot-interval-sec=3600" in flags
    assert "--storage-delta-on-identical-property-update=false" in flags
    assert "--log-min-duration-ms=1000" in flags


def test_memgraph_flags_keep_durability_on() -> None:
    flags = _memgraph_command()
    assert "--storage-wal-enabled=false" not in flags
    assert not any(
        flag.startswith("--storage-snapshot-interval-sec=") and flag.endswith("=0")
        for flag in flags
    )
    assert "--storage-snapshot-on-exit=false" not in flags


def test_addon_version_and_changelog_agree() -> None:
    config = yaml.safe_load((ADDON / "config.yaml").read_text())
    changelog = (ADDON / "CHANGELOG.md").read_text()
    assert f"## {config['version']}" in changelog
