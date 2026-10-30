"""ON-039: the add-on's Memgraph runtime flags work on the exact shipped image.

Memgraph runs with `strict_flag_check=true`, so one unknown or misspelt flag
makes it exit at start-up and the add-on watchdog would loop. These tests start
the image pinned in `memgraph_addon/Dockerfile` (by tag and digest, not
`latest`) with the flags from `memgraph_addon/supervisor.conf`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
import pytest_socket
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from custom_components.ontology.memgraph_client import MemgraphClient
from tests.integration.conftest import BOLT_PORT, _wait_for_bolt_port

ADDON = Path(__file__).parents[2] / "memgraph_addon"

# Path/process flags that only make sense inside the add-on's /data layout.
_ADDON_ONLY_FLAGS = ("--data-directory=", "--log-file=", "--bolt-port=")


def _shipped_image() -> str:
    match = re.search(
        r"^FROM (memgraph/memgraph:\S+@sha256:[0-9a-f]{64})", (ADDON / "Dockerfile").read_text(), re.M
    )
    assert match, "memgraph/memgraph image must be pinned by tag and digest"
    return match.group(1)


def _shipped_flags() -> list[str]:
    supervisor = (ADDON / "supervisor.conf").read_text()
    section = supervisor.split("[program:memgraph]", 1)[1].split("\n[program:", 1)[0]
    line = next(line for line in section.splitlines() if line.startswith("command="))
    tokens = line.removeprefix("command=").split()[1:]  # drop the executable
    return [t for t in tokens if not t.startswith(_ADDON_ONLY_FLAGS)]


def _start(flags: list[str]) -> DockerContainer:
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    container = (
        DockerContainer(_shipped_image())
        .with_exposed_ports(BOLT_PORT)
        .with_command(" ".join(flags))
    )
    container.start()
    wait_for_logs(container, "You are running Memgraph", timeout=60)
    _wait_for_bolt_port(
        container.get_container_host_ip(), int(container.get_exposed_port(BOLT_PORT))
    )
    return container


@pytest.fixture(scope="module")
def addon_memgraph() -> DockerContainer:
    container = _start(_shipped_flags())
    yield container
    container.stop()


@pytest_asyncio.fixture
async def addon_client(
    addon_memgraph: DockerContainer, socket_enabled: None
) -> AsyncGenerator[MemgraphClient]:
    client = MemgraphClient(
        host=addon_memgraph.get_container_host_ip(),
        port=int(addon_memgraph.get_exposed_port(BOLT_PORT)),
    )
    await client.connect()
    yield client
    await client.close()


async def _config(client: MemgraphClient) -> dict[str, str]:
    rows = await client.run_query("SHOW CONFIG")
    return {row["name"]: row["current_value"] for row in rows}


def test_shipped_flags_are_parsed_from_supervisor_conf() -> None:
    flags = _shipped_flags()
    assert "--telemetry-enabled=false" in flags
    assert not any(flag.startswith("--data-directory") for flag in flags)


async def test_memgraph_starts_and_applies_the_shipped_flags(
    addon_client: MemgraphClient,
) -> None:
    # Reaching this point proves strict_flag_check accepted every flag.
    config = await _config(addon_client)

    assert config["telemetry_enabled"] == "false"
    assert config["storage_snapshot_interval_sec"] == "3600"
    assert config["storage_delta_on_identical_property_update"] == "false"
    assert config["log_min_duration_ms"] == "1000"


async def test_durability_settings_are_still_on(addon_client: MemgraphClient) -> None:
    config = await _config(addon_client)

    assert config["storage_wal_enabled"] == "true"
    assert config["storage_snapshot_on_exit"] == "true"
    assert config["storage_snapshot_retention_count"] == "3"


def _docker(*args: str) -> str:
    # The HA test plugin blocks the Docker API unix socket inside tests, so the
    # restart goes through the docker CLI in a subprocess instead.
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


async def test_data_survives_a_graceful_restart(
    addon_memgraph: DockerContainer, addon_client: MemgraphClient
) -> None:
    await addon_client.run_query("CREATE (:DurabilityProbe {ha_id: 'keep-me', value: 42})")
    await addon_client.close()

    container_id = addon_memgraph.get_wrapped_container().id
    _docker("stop", "-t", "30", container_id)  # SIGTERM: Memgraph snapshots on exit
    _docker("start", container_id)
    host_port = int(_docker("port", container_id, f"{BOLT_PORT}/tcp").splitlines()[0].rsplit(":", 1)[1])
    host = addon_memgraph.get_container_host_ip()
    _wait_for_bolt_port(host, host_port)

    client = MemgraphClient(host=host, port=host_port)
    await client.connect()
    try:
        rows = await client.run_query(
            "MATCH (n:DurabilityProbe {ha_id: 'keep-me'}) RETURN n.value AS value"
        )
    finally:
        await client.close()
    assert rows == [{"value": 42}]
