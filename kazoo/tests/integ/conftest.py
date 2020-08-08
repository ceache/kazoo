from __future__ import annotations

import functools
import os
import uuid
from typing import TYPE_CHECKING

import pytest

from kazoo.client import KazooClient
from kazoo.testing.kazoo_ensemble import (
    KazooZkEnv,
    ZkEnsemble,
    check_skip_version_marker,
    docker_env,
    pytest_configure,
)
from kazoo.testing.kazoo_ensemble import (
    pytest_configure as kazoo_ensemble_pytest_configure,
)

if TYPE_CHECKING:
    from typing import Generator

    from pytest_docker import Services

    from kazoo.client import KazooClient


# This function is now imported from kazoo.testing.kazoo_ensemble,
# but we need a local stub to satisfy pytest's plugin discovery.
def pytest_configure(config):
    kazoo_ensemble_pytest_configure(config)


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig) -> str:
    del pytestconfig
    return os.path.join(os.path.dirname(__file__), "docker-compose.yml")


@pytest.fixture(scope="session")
def zkensemble(
    docker_ip: str,
    docker_services: Services,
    docker_env: KazooZkEnv,
) -> ZkEnsemble:
    """Ensure that HTTP service is up and responsive."""

    # `port_for` takes a container port and returns the corresponding host port
    zk1_port = docker_services.port_for("zoo1", 2181)
    zk2_port = docker_services.port_for("zoo2", 2181)
    zk3_port = docker_services.port_for("zoo3", 2181)

    return ZkEnsemble(
        zk_ip=docker_ip,
        zk1_port=zk1_port,
        zk2_port=zk2_port,
        zk3_port=zk3_port,
        version=docker_env.version,
        docker_services=docker_services,
    )


@pytest.fixture(scope="function")
def zkclient(
    request: pytest.FixtureRequest,
    zkensemble: ZkEnsemble,
) -> Generator[KazooClient, None, None]:
    """Create a KazooClient instance connected to the ensemble."""
    chroot = f"/{os.path.basename(request.node.nodeid)}"
    client = zkensemble.get_client()
    client.harness_expire_session = functools.partial(
        zkensemble.expire_session,
        client=client,
        event_factory=client.handler.event_object,
    )
    client.start()
    client.ensure_path(chroot)
    client.chroot = chroot
    yield client
    client.stop()
    client.close()


@pytest.fixture(scope="function")
def zksuperadmin_client(
    request: pytest.FixtureRequest,
    zkensemble: ZkEnsemble,
) -> KazooClient:
    """Create a KazooClient instance connected as superadmin to the ensemble."""
    chroot = f"/{request.node.name}-{uuid.uuid4()}-superadmin"
    client = zkensemble.get_client(superadmin=True)
    client.start()
    client.ensure_path(chroot)
    client.chroot = chroot
    yield client
    client.stop()
    client.close()
