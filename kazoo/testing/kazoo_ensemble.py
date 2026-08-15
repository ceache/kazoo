from __future__ import annotations

import functools
import os
import pathlib
import subprocess
import uuid
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
    from testcontainers.compose import DockerCompose

    from kazoo.client import KazooClient

# The three testing axes. The "auth" axis selects the docker-compose flavor
# (and therefore which client-side connection options make sense), while the
# "features" axis controls ZooKeeper JVM/system flags.
ZK_AUTH_MODES: tuple[str, ...] = (
    "plain",
    "digest",
    "sasl_digest",
    "sasl_gssapi",
    "tls",
)
ZK_FEATURES: tuple[str, ...] = (
    "standard",
    "ttl",
    "readonly",
    "reconfig",
)
ZK_DEFAULT_VERSION = "3.9.4"

# feature -> JVM/system properties (injected into the server environment)
FEATURE_JVM_PROPERTIES: dict[str, tuple[str, ...]] = {
    "standard": (),
    "ttl": ("-Dzookeeper.extendedTypesEnabled=true",),
    "readonly": ("-Dzookeeper.readonlymode.enabled=true",),
    "reconfig": ("-Dzookeeper.reconfigEnabled=true",),
}

# auth -> JVM/system properties (injected into the server environment).
# These are exported to the compose environment as ZK_AUTH_JVMFLAGS and
# interpolated into SERVER_JVMFLAGS by the base compose file.
AUTH_JVM_FLAGS: dict[str, str] = {
    "plain": "",
    "digest": (
        "-Dzookeeper.DigestAuthenticationProvider.superDigest="
        '"super:D/InIHSb7yEEbrWz8b9l71RjZJU="'
    ),
    "sasl_digest": "",
    "sasl_gssapi": "",
    "tls": "",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register CLI options for the three testing axes."""
    parser.addoption(
        "--zk-version",
        action="store",
        default=None,
        help=(
            "ZooKeeper server version (e.g. 3.7, 3.8, 3.9). "
            "Defaults to $ZK_VERSION or '3.9.4'."
        ),
    )
    parser.addoption(
        "--zk-auth",
        action="store",
        default=None,
        choices=list(ZK_AUTH_MODES),
        help=(
            "ZooKeeper authentication flavor: plain, digest, sasl_digest, "
            "sasl_gssapi, tls. Defaults to $ZK_AUTH or 'plain'."
        ),
    )
    parser.addoption(
        "--zk-features",
        action="store",
        default=None,
        help=(
            "Comma-separated ZooKeeper feature set: standard, ttl, readonly, "
            "reconfig. Defaults to $ZK_FEATURES or 'standard'."
        ),
    )


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
    auth: str = "plain"
    features: tuple[str, ...] = ("standard",)


@attrs.frozen(kw_only=True, auto_attribs=True)
class ZkEnsemble:
    zk_ip: str
    zk1_port: int
    zk2_port: int
    zk3_port: int
    # ports: list[int]
    version: str
    compose: DockerCompose
    auth: str = "plain"
    features: tuple[str, ...] = ("standard",)

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

        # Apply connection options implied by the active auth axis, unless the
        # caller has already provided an explicit value.
        if self.auth != "plain":
            if (
                "use_ssl" not in kwargs
                and "auth_data" not in kwargs
                and "sasl_options" not in kwargs
            ):
                if self.auth == "tls":
                    kwargs["use_ssl"] = True
                elif self.auth == "digest":
                    kwargs["auth_data"] = [("digest", "super:super_secret")]
                elif self.auth in ("sasl_digest", "sasl_gssapi"):
                    kwargs["sasl_options"] = {
                        "mechanism": (
                            "DIGEST-MD5"
                            if self.auth == "sasl_digest"
                            else "GSSAPI"
                        )
                    }

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

    def _run_compose(self, *args: str) -> None:
        """Run a ``docker compose`` command against this ensemble's stack."""
        subprocess.run(
            [*self.compose.compose_command_property, *args],
            cwd=self.compose.context,
            check=True,
        )

    def stop(self, name: str) -> None:
        """Stop the specified ZK node."""
        self._run_compose("stop", name)

    def start(self, name: str) -> None:
        """Start the specified ZK node."""
        self._run_compose("start", name)


#: Module-global handle on the running compose stack, set by the
#: :func:`docker_compose` fixture and consumed by :func:`dump_ensemble_logs`
#: while the stack is still up.
_COMPOSE_HANDLE: DockerCompose | None = None


def _ensure_docker_available(context: str) -> None:
    """Fail fast with an actionable message if docker compose is unavailable."""
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            cwd=context,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "The 'docker' CLI was not found on PATH; the kazoo integration "
            "tests require a Docker Engine with the Compose v2 plugin "
            "(see https://docs.docker.com/compose/install/)."
        ) from None
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "`docker compose version` failed; the Compose v2 plugin is "
            "required (Compose v2.12+ for `up --wait`)."
        ) from None

    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise RuntimeError(
            "`docker info` failed; is the Docker daemon running? The kazoo "
            "integration tests require a running Docker Engine."
        ) from None


def dump_ensemble_logs() -> None:
    """Dump stdout/stderr of every ensemble member to aid failure diagnosis.

    Best-effort only: a stack that is mid-teardown or already removed will
    simply log nothing. Called while the compose stack is still running.
    """
    compose = _COMPOSE_HANDLE
    if compose is None:
        return

    def _print_logs(service: str) -> None:
        try:
            stdout, stderr = compose.get_logs(service)
        except Exception as exc:  # noqa: BLE001 - best-effort log dump
            print(f"\n[kazoo] failed to fetch logs for {service}: {exc!r}")
            return
        for label, stream in (("stdout", stdout), ("stderr", stderr)):
            text = (
                stream.decode("utf-8", "replace")
                if isinstance(stream, bytes)
                else stream
            )
            print(f"\n===== {service} {label} =====")
            print(text)

    for service in ("zoo1", "zoo2", "zoo3"):
        _print_logs(service)


def _resolve_axis_options(
    pytestconfig: pytest.Config,
) -> tuple[str, str, tuple[str, ...]]:
    """Resolve the three axes from CLI options, falling back to env vars."""
    version = pytestconfig.getoption("--zk-version") or os.environ.get(
        "ZK_VERSION", ZK_DEFAULT_VERSION
    )
    auth = pytestconfig.getoption("--zk-auth") or os.environ.get(
        "ZK_AUTH", "plain"
    )
    features_str = pytestconfig.getoption("--zk-features") or os.environ.get(
        "ZK_FEATURES", "standard"
    )
    features = tuple(f.strip() for f in features_str.split(",") if f.strip())
    # Make the resolved values available to docker-compose interpolation.
    os.environ["ZK_VERSION"] = version
    os.environ["ZK_AUTH"] = auth
    os.environ["ZK_FEATURES"] = ",".join(features)
    os.environ["ZK_AUTH_JVMFLAGS"] = AUTH_JVM_FLAGS.get(auth, "")
    return version, auth, features


@pytest.fixture(scope="session", autouse=True)
def docker_env(
    pytestconfig: pytest.Config,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[KazooZkEnv]:
    with tmp_path_factory.getbasetemp() as tmp_path:
        os.environ["ZK_WORK_DIR"] = str(tmp_path)
        # Unique per-session compose project name keeps parallel test runs (and
        # any stray stacks from other projects) isolated from each other.
        os.environ["COMPOSE_PROJECT_NAME"] = f"kazoo-{uuid.uuid4().hex[:8]}"
        version, auth, features = _resolve_axis_options(pytestconfig)
        yield KazooZkEnv(
            version=version,
            workdir=tmp_path,
            auth=auth,
            features=features,
        )


@pytest.fixture(scope="session")
def docker_compose(
    request: pytest.FixtureRequest,
    docker_compose_config: dict[str, Any],
) -> Iterator[DockerCompose]:
    """Start the ZooKeeper ensemble stack via docker-compose (testcontainers).

    Session-scoped: the ensemble is brought up once before the first test and
    torn down (including volumes) after the last test. Individual ensemble
    members are controlled per-test through :meth:`ZkEnsemble.stop` /
    :meth:`ZkEnsemble.start`.

    The ``testcontainers.compose.DockerCompose`` driver is imported lazily so
    that ``kazoo.testing`` stays importable in environments where the test-only
    dependency is not installed.
    """
    from testcontainers.compose import DockerCompose

    # compose files live next to the integration tests (conftest.py location).
    context = str(
        pathlib.Path(__file__).resolve().parent.parent / "tests" / "integ"
    )
    _ensure_docker_available(context)

    compose = DockerCompose(
        context=context,
        compose_file_name=docker_compose_config["compose_files"],
    )

    global _COMPOSE_HANDLE
    try:
        compose.start()
        _COMPOSE_HANDLE = compose
        # Belt-and-suspenders beyond `up --wait`: fail fast with a precise
        # message if any ensemble member is not actually healthy.
        for node in ("zoo1", "zoo2", "zoo3"):
            container = compose.get_container(node)
            if container.Health != "healthy":
                raise RuntimeError(
                    f"{node} did not become healthy after `docker compose up "
                    f"--wait` (state={container.State!r}, "
                    f"health={container.Health!r})"
                )
        yield compose
    finally:
        # Runs even when `start()` itself raised partway (e.g. one node never
        # became healthy), so `down --volumes` always cleans up the stack.
        if request.session.testsfailed:
            dump_ensemble_logs()
        _COMPOSE_HANDLE = None
        compose.stop()


@pytest.fixture(scope="function")
def zkensemble(
    docker_compose: DockerCompose,
    docker_env: KazooZkEnv,
) -> ZkEnsemble:
    """Provide a per-test handle on the running ZooKeeper ensemble.

    Unlike a session-scoped handle, this fixture is created fresh for every
    test so that each test can create its own clients and control individual
    ensemble members (e.g. stop/start via :meth:`ZkEnsemble.stop`).
    """

    # The ensemble exposes its client ports on ephemeral host ports; resolve
    # the actual host address/ports via the running compose stack.
    zk1_port = docker_compose.get_service_port("zoo1", 2181)
    zk2_port = docker_compose.get_service_port("zoo2", 2181)
    zk3_port = docker_compose.get_service_port("zoo3", 2181)

    return ZkEnsemble(
        zk_ip=docker_compose.get_service_host("zoo1", 2181),
        zk1_port=zk1_port,
        zk2_port=zk2_port,
        zk3_port=zk3_port,
        version=docker_env.version,
        auth=docker_env.auth,
        features=docker_env.features,
        compose=docker_compose,
    )


@pytest.fixture(scope="function")
def zkchroot(request: pytest.FixtureRequest) -> str:
    """Unique per-test chroot path within the active ensemble."""
    return f"/{os.path.basename(request.node.nodeid)}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def zkclient(
    zkensemble: ZkEnsemble,
    zkchroot: str,
) -> Iterator[KazooClient]:
    """Create a KazooClient instance connected to the ensemble."""
    client = zkensemble.get_client()
    client.harness_expire_session = functools.partial(
        zkensemble.expire_session,
        client=client,
        event_factory=client.handler.event_object,
    )
    client.start()
    client.ensure_path(zkchroot)
    client.chroot = zkchroot
    yield client
    client.stop()
    client.close()


@pytest.fixture(scope="function")
def zksuperadmin_client(
    request: pytest.FixtureRequest,
    zkensemble: ZkEnsemble,
) -> Iterator[KazooClient]:
    """Create a KazooClient instance connected as superadmin to the ensemble."""
    chroot = f"/{os.path.basename(request.node.nodeid)}-{uuid.uuid4().hex[:8]}-superadmin"
    client = zkensemble.get_client(superadmin=True)
    client.start()
    client.ensure_path(chroot)
    client.chroot = chroot
    yield client
    client.stop()
    client.close()


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
