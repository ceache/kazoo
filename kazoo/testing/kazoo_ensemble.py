from __future__ import annotations

import os
import pathlib
from typing import TYPE_CHECKING

import attrs
import pytest
from packaging import (
    specifiers,
    version,
)

import kazoo.client
from kazoo.protocol.connection import (
    _CONNECTION_DROP,
    _SESSION_EXPIRED,
)
from kazoo.protocol.states import KazooState

if TYPE_CHECKING:
    from collections.abc import (
        Iterator,
    )
    from typing import (
        Any,
        Callable,
        Literal,
    )
    from threading import Event
    from pytest_docker import Services

    from kazoo.client import KazooClient


def pytest_configure(config):
    """
    Registers our custom marker so pytest knows about it.
    """
    config.addinivalue_line(
        "markers",
        "skip_if_zk_version(condition): Skip test based on the 'zkensemble' fixture's version.",
    )


@attrs.frozen(kw_only=True, auto_attribs=True)
class KazooZkEnv:
    version: str
    workdir: pathlib.Path


@attrs.frozen(kw_only=True, auto_attribs=True)
class ZkEnsemble:
    zk_ip: str
    zk1_port: int
    zk2_port: int
    zk3_port: int
    # ports: list[int]
    version: str
    docker_services: Services

    def get_hosts(self) -> str:
        client_hosts = ",".join(
            [
                f"{self.zk_ip}:{port}"
                for port in [self.zk1_port, self.zk2_port, self.zk3_port]
            ]
        )
        return client_hosts

    def get_client(self, /, superadmin: bool = False, **kwargs: Any) -> KazooClient:
        if "hosts" in kwargs:
            client_hosts = kwargs.pop("hosts")
        else:
            client_hosts = self.get_hosts()

        if superadmin:
            # For superadmin, the Zookeeper server must be configured with digest authentication.
            # This typically involves setting JVMFLAGS like:
            # -Dzookeeper.DigestAuthenticationProvider.superDigest="super:D/InIHSb7yEEbrWz8b9l71RjZJU="
            # in the server's startup script or docker-compose.yml.
            # The client then authenticates with the cleartext password "super_secret".
            auth_data = kwargs.pop("auth_data", None)
            if auth_data is None:
                kwargs["auth_data"] = [("digest", "super:super_secret")]
            else:
                if isinstance(auth_data, list):
                    auth_data.append(("digest", "super:super_secret"))
                    kwargs["auth_data"] = auth_data
                else:
                    raise ValueError(
                        "Existing 'auth_data' in kwargs must be a list of (scheme, credentials) tuples if 'superadmin' is True."
                    )

        client = kazoo.client.KazooClient(
            hosts=client_hosts,
            **kwargs,
        )
        return client

    def lose_connection(
        self, client: KazooClient, event_factory: Callable[[], Event] | None = None
    ) -> None:
        """Force client to lose connection with server"""
        if event_factory is None:
            event_factory = client.handler.event_object
        self.__break_connection(
            client, _CONNECTION_DROP, KazooState.SUSPENDED, event_factory
        )

    def expire_session(
        self, client: KazooClient, event_factory: Callable[[], Event] | None = None
    ) -> None:
        """Force ZK to expire a client session"""
        if event_factory is None:
            event_factory = client.handler.event_object
        self.__break_connection(
            client, _SESSION_EXPIRED, KazooState.LOST, event_factory
        )

    def __break_connection(
        self,
        client: KazooClient,
        break_event: object,
        expected_state: KazooState,
        event_factory: Callable[[], Event],
    ) -> None:
        """Break ZooKeeper connection using the specified event."""

        assert break_event in (_CONNECTION_DROP, _SESSION_EXPIRED)

        lost = event_factory()
        safe = event_factory()

        def watch_loss(state: KazooState) -> bool | None:
            if state == expected_state:
                lost.set()
            elif lost.is_set() and state == KazooState.CONNECTED:
                safe.set()
                return True
            return None

        client.add_listener(watch_loss)
        client._call(break_event, None)

        lost.wait(5)
        if not lost.is_set():
            raise Exception("Failed to get notified of broken connection.")

        safe.wait(15)
        if not safe.is_set():
            raise Exception("Failed to see client reconnect.")

        client.retry(client.get_async, "/")

    def stop(self, name: str) -> None:
        """Stop the specified ZK node."""
        self.docker_services._docker_compose.execute(f"stop {name}")

    def start(self, name: str) -> None:
        """Stop the specified ZK node."""
        self.docker_services._docker_compose.execute(f"start {name}")


@pytest.fixture(scope="session", autouse=True)
def docker_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[KazooZkEnv]:
    with tmp_path_factory.getbasetemp() as tmp_path:
        os.environ["ZK_WORK_DIR"] = str(tmp_path)
        version = os.environ.get("ZK_VERSION", "3.9.4")
        os.environ["ZK_VERSION"] = version
        yield KazooZkEnv(
            version=version,
            workdir=tmp_path,
        )


@pytest.fixture(autouse=True)
def check_skip_version_marker(
    request: pytest.FixtureRequest,
    docker_env: KazooZkEnv,
) -> None:
    """
    This is the "magic" fixture. It runs for every test.
    1. It looks for our custom marker on the test.
    2. If it finds it, it checks the condition against the 'my_data' fixture.
    3. It calls pytest.skip() if the condition is met.
    """
    marker = request.node.get_closest_marker("skip_if_zk_version")
    if not marker:
        # The test doesn't have our marker, so we do nothing.
        return

    # Get the condition from the marker, e.g., "<3.4"
    condition_string = marker.args[0]
    specifier = specifiers.SpecifierSet(condition_string)

    # Get the actual version from our data fixture
    zkversion = version.Version(docker_env.version)

    if zkversion in specifier:
        pytest.skip(
            f"Skipped: Zookeeper ensemble version {zkversion} matches '{specifier}'"
        )
