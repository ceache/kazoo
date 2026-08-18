from __future__ import annotations

import functools
import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid
from importlib import resources
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from backports.strenum import StrEnum

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
    )
    from threading import Event
    from testcontainers.compose import DockerCompose

    from kazoo.client import KazooClient


# The three testing axes. The "auth" axis selects the docker-compose flavor
# (and therefore which client-side connection options make sense), while the
# "features" axis controls ZooKeeper JVM/system flags.
class ZKAuthMode(StrEnum):
    PLAIN = "plain"
    DIGEST = "digest"
    SASL_DIGEST = "sasl_digest"
    SASL_GSSAPI = "sasl_gssapi"
    TLS = "tls"


class ZKFeature(StrEnum):
    STANDARD = "standard"
    TTL = "ttl"
    READONLY = "readonly"
    RECONFIG = "reconfig"
    # Harness-level feature: adds the capture sidecar to the compose stack.
    # Deliberately absent from FEATURE_JVM_PROPERTIES below — capture is a
    # harness observation feature, not a ZooKeeper server feature, so it must
    # contribute no server JVM flags (FR-007, R-04).
    CAPTURE = "capture"


ZK_DEFAULT_VERSION = "3.9.5"

# feature -> JVM/system properties (injected into the server environment)
FEATURE_JVM_PROPERTIES: dict[ZKFeature, tuple[str, ...]] = {
    ZKFeature.STANDARD: (),
    ZKFeature.TTL: ("-Dzookeeper.extendedTypesEnabled=true",),
    ZKFeature.READONLY: ("-Dzookeeper.readonlymode.enabled=true",),
    ZKFeature.RECONFIG: ("-Dzookeeper.reconfigEnabled=true",),
}

# auth -> JVM/system properties (injected into the server environment).
# These are exported to the compose environment as ZK_AUTH_JVMFLAGS and
# interpolated into SERVER_JVMFLAGS by the base compose file.
AUTH_JVM_FLAGS: dict[ZKAuthMode, str] = {
    ZKAuthMode.PLAIN: "",
    ZKAuthMode.DIGEST: (
        "-Dzookeeper.DigestAuthenticationProvider.superDigest="
        '"super:D/InIHSb7yEEbrWz8b9l71RjZJU="'
    ),
    ZKAuthMode.SASL_DIGEST: "",
    ZKAuthMode.SASL_GSSAPI: "",
    ZKAuthMode.TLS: "",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register CLI options for the three testing axes."""
    parser.addoption(
        "--zk-version",
        action="store",
        default=None,
        help=(
            "ZooKeeper server version (e.g. 3.7, 3.8, 3.9). "
            "Defaults to $ZK_VERSION or '3.9.5'."
        ),
    )
    parser.addoption(
        "--zk-auth",
        action="store",
        default=None,
        choices=[mode.value for mode in ZKAuthMode],
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
            "reconfig, capture. Defaults to $ZK_FEATURES or 'standard'."
        ),
    )


def pytest_configure(config):
    """
    Registers our custom markers so pytest knows about them.
    """
    config.addinivalue_line(
        "markers",
        "skip_if_zk_version(condition): Skip test based on the "
        "'zkensemble' fixture's version.",
    )
    config.addinivalue_line(
        "markers",
        "zk_version(spec): Run only when the active ZK version matches "
        "the PEP 440 SpecifierSet.",
    )
    config.addinivalue_line(
        "markers",
        "zk_auth(*allowed, skip=None): Run only under the listed auth "
        "schemes, or skip the listed ones.",
    )
    config.addinivalue_line(
        "markers",
        "zk_features(require=None, skip=None): Run only when all `require` "
        "features are active and none of `skip` are.",
    )


def _evaluate_axis_markers(
    item: pytest.Item,
    zk_version: str,
    auth: ZKAuthMode,
    features: tuple[ZKFeature, ...],
) -> str | None:
    """Evaluate the zk_version/zk_auth/zk_features markers on a test item.

    Returns an actionable skip reason string, or ``None`` when the item is
    compatible with the active run configuration.
    """
    reasons: list[str] = []

    marker = item.get_closest_marker("zk_version")
    if marker:
        spec = marker.args[0]
        if version.Version(zk_version) not in specifiers.SpecifierSet(spec):
            reasons.append(f"Requires ZK {spec} (active: {zk_version})")

    marker = item.get_closest_marker("zk_auth")
    if marker:
        # Marker args are plain strings (e.g. @pytest.mark.zk_auth("digest"));
        # StrEnum members compare equal to their value, so membership tests
        # against those strings work unchanged.
        allowed = marker.args or ()
        skip = marker.kwargs.get("skip") or ()
        if allowed and auth.value not in allowed:
            reasons.append(
                f"Requires auth in {sorted(allowed)} (active: {auth.value})"
            )
        if auth.value in skip:
            reasons.append(f"Incompatible with auth {auth.value}")

    marker = item.get_closest_marker("zk_features")
    if marker:
        require = marker.kwargs.get("require") or ()
        skip_features = marker.kwargs.get("skip") or ()
        active = {f.value for f in features}
        missing = [f for f in require if f not in active]
        if missing:
            reasons.append(f"Missing required feature(s): {missing}")
        incompatible = [f for f in skip_features if f in active]
        if incompatible:
            reasons.append(
                f"Incompatible with active feature(s): {incompatible}"
            )

    return "; ".join(reasons) if reasons else None


def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Apply collection-time skip evaluation for the axis markers.

    Incompatible tests are skipped before any client/ensemble is spun up, so
    they never attempt connections (FR-008, SC-005). The legacy
    ``skip_if_zk_version`` marker keeps its function-scoped evaluation via the
    ``check_skip_version_marker`` autouse fixture.
    """
    version, auth, features = _resolve_axis_options(config)
    for item in items:
        reason = _evaluate_axis_markers(item, version, auth, features)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int | pytest.ExitCode
) -> None:
    """Document the interrupted-session artifact guarantee (quickstart V9).

    When a capture-enabled run is interrupted mid-suite (keyboard interrupt),
    Docker's ``down`` teardown still runs the session fixture (R-05/R-07), so
    the capture sidecars receive SIGTERM and flush their pcapng files before
    exiting. The bind-mounted artifacts therefore survive the interruption as
    readable *partial* files (FR-003 edge). This hook verifies that reality
    post-hoc: on interruption it best-effort probes the newest per-member
    pcapng for a readable Section Header Block and reports it. Best-effort
    only — the run is already interrupted and must never turn into a failure.
    """
    if exitstatus != getattr(pytest.ExitCode, "INTERRUPTED", 130):
        return
    workdir = os.environ.get("ZK_WORK_DIR")
    captures = (
        pathlib.Path(workdir) / "captures"
        if workdir
        else pathlib.Path(session.config.rootpath) / "captures"
    )
    if not captures.is_dir():
        return
    magics = (b"\x0a\x0d\x0d\x0a", b"\x4d\x3c\xb2\xa1")
    readable = []
    for pattern in (
        "kazoo-client-zoo1-*.pcapng",
        "kazoo-client-zoo2-*.pcapng",
        "kazoo-client-zoo3-*.pcapng",
    ):
        files = sorted(captures.glob(pattern))
        if not files:
            continue
        try:
            with files[-1].open("rb") as handle:
                if handle.read(4) in magics:
                    readable.append(files[-1].name)
        except OSError:
            pass
    if readable:
        print(
            "[kazoo] interrupted capture left readable partial artifacts: "
            + ", ".join(readable)
        )


@attrs.frozen(kw_only=True, auto_attribs=True)
class KazooZkEnv:
    version: str
    workdir: pathlib.Path
    auth: ZKAuthMode = ZKAuthMode.PLAIN
    features: tuple[ZKFeature, ...] = (ZKFeature.STANDARD,)


@attrs.frozen(kw_only=True, auto_attribs=True)
class ZkEnsemble:
    zk_ip: str
    zk1_port: int
    zk2_port: int
    zk3_port: int
    # ports: list[int]
    version: str
    compose: DockerCompose
    workdir: pathlib.Path
    auth: ZKAuthMode = ZKAuthMode.PLAIN
    features: tuple[ZKFeature, ...] = (ZKFeature.STANDARD,)

    def get_hosts(self) -> str:
        client_hosts = ",".join(
            [
                f"{self.zk_ip}:{port}"
                for port in [self.zk1_port, self.zk2_port, self.zk3_port]
            ]
        )
        return client_hosts

    def _client_implied_options(self) -> dict[str, Any]:
        """Connection options implied by the active auth axis.

        Each implied option is returned under its KazooClient kwarg name and
        is applied independently (see ``get_client``) so that, for example, a
        superadmin client (which supplies its own ``auth_data``) still gets the
        ``sasl_options``/``use_ssl`` implied by a SASL or TLS axis (FR-004,
        contracts/client-connection.md).
        """
        opts: dict[str, Any] = {}
        if self.auth is ZKAuthMode.DIGEST:
            opts["auth_data"] = [("digest", "super:super_secret")]
        elif self.auth is ZKAuthMode.SASL_DIGEST:
            opts["sasl_options"] = {
                "mechanism": "DIGEST-MD5",
                # DigestServerCallback in the server JAAS config only accepts
                # the hardcoded test users (see jaas/sasl-digest.conf).
                "username": "jaasuser",
                "password": "jaas_password",
            }
        elif self.auth in (ZKAuthMode.TLS, ZKAuthMode.SASL_GSSAPI):
            # TLS transport: client cert + CA produced by the certgen sidecar
            # (see dockerfiles/certgen; sasl_gssapi tunnels GSSAPI over TLS per
            # FR-012). The bundle carries the key followed by the certificate,
            # so it serves as both certfile and keyfile.
            certs = self.workdir / "certs" / "client"
            opts["use_ssl"] = True
            opts["certfile"] = str(certs / "client.pem")
            opts["keyfile"] = str(certs / "client.pem")
            opts["ca"] = str(certs / "cacert.pem")
            if self.auth is ZKAuthMode.SASL_GSSAPI:
                opts["sasl_options"] = {"mechanism": "GSSAPI"}
        return opts

    def get_client(
        self, /, superadmin: bool = False, **kwargs: Any
    ) -> KazooClient:
        if "hosts" in kwargs:
            client_hosts = kwargs.pop("hosts")
        else:
            client_hosts = self.get_hosts()

        if superadmin:
            # For superadmin, the Zookeeper server must be configured with
            # digest authentication. This typically involves setting JVMFLAGS
            # like:
            # -Dzookeeper.DigestAuthenticationProvider.superDigest="super:D/InIHSb7yEEbrWz8b9l71RjZJU="
            # in the server's startup script or docker-compose.yml.
            # The client then authenticates with the cleartext password
            # "super_secret".
            auth_data = kwargs.pop("auth_data", None)
            if auth_data is None:
                kwargs["auth_data"] = [("digest", "super:super_secret")]
            else:
                if isinstance(auth_data, list):
                    auth_data.append(("digest", "super:super_secret"))
                    kwargs["auth_data"] = auth_data
                else:
                    raise ValueError(
                        "Existing 'auth_data' in kwargs must be a list of "
                        "(scheme, credentials) tuples if 'superadmin' is True."
                    )

        # Apply connection options implied by the active auth axis. Each option
        # is set only if the caller did not already provide it explicitly, so
        # an explicit override always wins and no implied option silently
        # clobbers another (e.g. superadmin's auth_data coexists with the
        # sasl_options a SASL axis requires).
        for key, value in self._client_implied_options().items():
            kwargs.setdefault(key, value)

        client = kazoo.client.KazooClient(
            hosts=client_hosts,
            **kwargs,
        )
        return client

    def lose_connection(
        self,
        client: KazooClient,
        event_factory: Callable[[], Event] | None = None,
    ) -> None:
        """Force client to lose connection with server"""
        if event_factory is None:
            event_factory = client.handler.event_object
        self.__break_connection(
            client, _CONNECTION_DROP, KazooState.SUSPENDED, event_factory
        )

    def expire_session(
        self,
        client: KazooClient,
        event_factory: Callable[[], Event] | None = None,
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

    @staticmethod
    def _process_service(name: str) -> str:
        """Map a member name to the compose service running its ZK JVM.

        Under the network-holder split (docker-compose.base.yml), the compose
        service ``zooN`` is only a netns-holding container while the actual
        ZooKeeper process lives in ``zooN-service``. Failure-injection tests
        stop/start a member's ZooKeeper *process*, so any member name must be
        translated to its ``-service`` twin here; the holder itself is never
        stopped (it keeps the member's network namespace — and the capture
        sidecar's tap — alive across member restarts).
        """
        if name in {"zoo1", "zoo2", "zoo3"}:
            return f"{name}-service"
        return name

    def stop(self, name: str) -> None:
        """Stop the specified ZK node's ZooKeeper process."""
        self._run_compose("stop", self._process_service(name))

    def start(self, name: str) -> None:
        """Start the specified ZK node's ZooKeeper process."""
        self._run_compose("start", self._process_service(name))


#: Module-global handle on the running compose stack, set by the
#: :func:`docker_compose` fixture and consumed by :func:`dump_ensemble_logs`
#: while the stack is still up.
_COMPOSE_HANDLE: DockerCompose | None = None


def _ensure_docker_available(context: str) -> None:
    """Fail fast if docker compose is unavailable."""
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
    _ensure_linux_docker_backend()


def _daemon_mount_path(path: pathlib.Path) -> str:
    """Return ``path`` as a bind-mount source the docker daemon can see.

    Bind mounts are resolved by the *daemon* host, so a Windows client that
    talks to a remote Linux engine over TCP (``DOCKER_HOST=tcp://...`` — e.g.
    a WSL2-hosted dockerd on the GitHub Windows runner) must expose its
    drives at the daemon's `/mnt/<drive>` mount points rather than as
    Windows-style paths (``D:/a/b``). Docker Desktop's own WSL2 backend uses
    the same ``/mnt`` layout, so this stays valid there too. When the client
    targets a native engine (Docker Desktop, local Linux), the host path is
    passed through unchanged (FR-011).
    """
    posix = path.as_posix()
    host = os.environ.get("DOCKER_HOST", "")
    if os.name == "nt" and host.startswith(("tcp://", "http://")):
        drive = re.match(r"^([A-Za-z]):(/.+)$", posix)
        if drive:
            return f"/mnt/{drive.group(1).lower()}{drive.group(2)}"
    return posix


def _ensure_linux_docker_backend() -> None:
    """Skip (never fail, SC-005) when the docker daemon is not a Linux backend.

    The official ZooKeeper image is published for Linux only, so a
    Windows-container daemon (e.g. the GitHub-hosted ``windows-latest``
    runner, whose Moby engine serves Windows containers) cannot pull it and
    ``compose up`` dies with ``no matching manifest for
    windows(...)/amd64``. Detecting the daemon OS up front turns that into a
    clean skip of the whole ensemble suite with an actionable reason instead
    of a wall of per-test errors (FR-011: a real Windows host with Docker
    Desktop's Linux backend passes normally).
    """
    try:
        ostype = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        # The daemon is reachable (checked above); an unusual driver just
        # means this probe is unsupported, so do not hard-fail on it.
        return
    if ostype and ostype.lower() != "linux":
        pytest.skip(
            f"docker engine OSType is {ostype!r}, not 'linux': the "
            "ZooKeeper official image is linux-only, so the ensemble suite "
            "cannot run against a Windows-container docker backend (SC-005). "
            "Run these tests with Docker Desktop (Linux containers) or a "
            "Linux Docker Engine."
        )


def _build_capture_images(compose: DockerCompose, context: str) -> None:
    """Build the in-repo capture image before the stack starts (R-07).

    ``docker compose up --wait`` would normally build the image declared by
    the overlay, but a failure there surfaces as an opaque error partway into
    ``start()``. Building explicitly first converts any build-time problem
    (Docker network / registry outage for ``apk`` tshark) into a single,
    actionable ``RuntimeError`` raised before the ensemble is even started.
    The session fixture's ``finally`` teardown still runs, so nothing is left
    behind.
    """
    # Reuse the same compose project/context/overlay list the stack will start
    # with; `build` is a bool flag on the driver that only modifies `up`, so
    # the build subcommand is invoked here directly.
    cmd = [*compose.docker_compose_command(), "build"]
    try:
        subprocess.run(
            cmd,
            cwd=context,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.decode("utf-8", "replace").strip() or (
            exc.stdout.decode("utf-8", "replace").strip()
        )
        raise RuntimeError(
            "capture: in-repo image build failed before the stack started "
            f"({details or exc}). Check Docker network/registry reachability "
            "for dockerfiles/capture (apk tshark)."
        ) from exc


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

    # Logs from the ZK JVM processes; the network-ns holders (zoo1..zoo3)
    # run no JVM, so their stdout/stderr carry nothing of interest.
    for service in ("zoo1-service", "zoo2-service", "zoo3-service"):
        _print_logs(service)


def _assemble_tls_keylog(
    workdir: pathlib.Path,
    auth: ZKAuthMode,
    features: tuple[ZKFeature, ...],
) -> list[pathlib.Path] | None:
    """Concatenate per-node TLS keylogs + context certs into ``captures/tls/``.

    On ``tls``+``capture`` runs the ensemble JVMs are launched with the
    ``extract-tls-secrets`` agent (R-02), which writes an SSLKEYLOGFILE-format
    secrets file into each node's ``/logs`` bind mount. This assembles those
    per-node keylogs (``logs/zk1|zk2|zk3/tls-secrets.log``) into
    ``captures/tls/zk-secrets.log`` and copies the server + CA certificates
    for context, so the pcapng artifacts can be decrypted (R-09, FR-006). No
    private key is exported, and the keylog contents are never printed.

    Returns the emitted artifact paths, or ``None`` when this run has no keylog
    (capture inactive or auth is not ``tls``).
    """
    if ZKFeature.CAPTURE not in features or auth is not ZKAuthMode.TLS:
        return None

    tls_dir = workdir / "captures" / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)

    keylog = tls_dir / "zk-secrets.log"
    with keylog.open("wb") as out:
        for log_dir in ("zk1", "zk2", "zk3"):
            node_log = workdir / "logs" / log_dir / "tls-secrets.log"
            if node_log.is_file() and node_log.stat().st_size:
                out.write(node_log.read_bytes())
                out.write(b"\n")

    emitted: list[pathlib.Path] = []
    copies = {
        workdir / "certs" / "server" / "server.pem": "server-cert.pem",
        workdir / "certs" / "cacert.pem": "ca.pem",
    }
    for source, name in copies.items():
        if source.is_file():
            destination = tls_dir / name
            shutil.copyfile(source, destination)
            emitted.append(destination)

    if keylog.stat().st_size:
        emitted.insert(0, keylog)
    if emitted:
        return emitted
    return None


def _export_krb5_client_env(
    docker_env: KazooZkEnv,
    docker_compose: DockerCompose,
) -> None:
    """Export the client-side Kerberos environment for the sasl_gssapi axis.

    The KDC sidecar writes a *server-view* ``krb5.conf`` (advertising
    ``kdc = kdc:1088``, resolvable only on the compose network) into the
    shared ``${ZK_WORK_DIR}`` bind mount. Host-side client processes cannot
    resolve the ``kdc`` compose service name, so this writes a *host-view*
    ``krb5.conf`` that points at the published KDC port and points the client
    at its keytab (FR-012, contracts/client-connection.md).

    The KDC publishes both TCP and UDP (``0:1088`` + ``0:1088/udp``) on the
    same host port; Docker Compose assigns both transports the same ephemeral
    port, so a single ``kdc = host:port`` line serves both. We resolve that
    port via the container's TCP publisher (``get_service_port`` itself would
    raise "not exactly 1 publisher" because the same TargetPort maps both
    protocols).

    The entry is written as plain ``kdc = host:port`` (no ``tcp/`` transport
    prefix): the ``tcp/`` prefix is Heimdal-only syntax, and MIT krb5 (the
    client on Linux CI) parses ``tcp/host:port`` as an unresolvable hostname
    (``Cannot contact any KDC``). MIT clients try UDP first and fall back to
    TCP, and modern Docker Desktop forwards UDP to containers, so the plain
    ``host:port`` entry reaches the KDC reliably on both implementations;
    both transports are published on the same host port (FR-012).

    The client is pointed at a *fresh per-run* FILE credential cache. Without
    this, macOS defaults to the shared ``API:...`` cache, which may still hold
    a TGT + service ticket (``zookeeper/127.0.0.1@EXAMPLE.ORG``) minted by a
    *previous* KDC instance (each compose stack runs its own realm with new
    keys). The client would reuse that stale ticket, and the fresh server
    cannot decrypt it (``Checksum failed`` / ``GSS initiate failed``). Heimdal
    ``kinit`` ignores ``KRB5CCNAME=FILE:...`` from the environment, so the
    cache is created explicitly with ``kinit -c <file> -kt <client.keytab>``.
    """
    from testcontainers.compose import PublishedPortModel

    container = docker_compose.get_container("kdc")
    publishers: list[PublishedPortModel] = container.Publishers
    tcp = [p for p in publishers if (p.Protocol or "").lower() == "tcp"]
    if not tcp:
        raise RuntimeError(
            "sasl_gssapi: no TCP publisher found for the KDC service; "
            "is docker-compose.auth-sasl-gssapi.yml being used?"
        )
    kdc_port = tcp[0].PublishedPort
    # ``normalize().URL`` returns the publisher's bind address (``0.0.0.0`` /
    # ``::`` on macOS/Linux), which host-side kinit cannot reach. Clients must
    # target the loopback interface where Docker publishes the port, exactly
    # like the ensemble host resolution below (see the ``zk_ip`` normalization
    # in the ``zkensemble`` fixture).
    kdc_host = tcp[0].normalize().URL
    if not kdc_host or kdc_host in ("0.0.0.0", "::", "::1", "localhost"):
        kdc_host = "127.0.0.1"

    host_krb5 = docker_env.workdir / "krb5.client.conf"
    host_krb5.write_text(
        f"[libdefaults]\n"
        f" default_realm = EXAMPLE.ORG\n"
        f" dns_lookup_realm = false\n"
        f" rdns = false\n"
        f"[realms]\n"
        f" EXAMPLE.ORG = {{\n"
        f"  kdc = {kdc_host}:{kdc_port}\n"
        f" }}\n",
        encoding="utf-8",
    )
    os.environ["KRB5_CONFIG"] = str(host_krb5)
    os.environ["KRB5_CLIENT_KTNAME"] = str(
        docker_env.workdir / "keytabs" / "client.keytab"
    )

    # Fresh per-run FILE credential cache for the client. See the docstring.
    ccache = docker_env.workdir / f"krb5cc-{os.getpid()}"
    ccache.unlink(missing_ok=True)
    kinit_env = dict(os.environ)
    # KRB5CCNAME must be a FILE cache for a kinit -c target; do not inherit a
    # stale API: cache location or the default macOS shared cache.
    kinit_env.pop("KRB5CCNAME", None)
    kinit_rc = subprocess.run(
        [
            "kinit",
            "-c",
            str(ccache),
            "-kt",
            os.environ["KRB5_CLIENT_KTNAME"],
            "client@EXAMPLE.ORG",
        ],
        capture_output=True,
        text=True,
        env=kinit_env,
    ).returncode
    if kinit_rc != 0:
        raise RuntimeError(
            "sasl_gssapi: host-side kinit failed; KDC unreachable from "
            f"client context (rc={kinit_rc}). See {host_krb5} and the "
            "transport-format note in the kazoo_ensemble module docstring."
        )
    os.environ["KRB5CCNAME"] = f"FILE:{ccache}"


def _resolve_axis_options(
    pytestconfig: pytest.Config,
) -> tuple[str, ZKAuthMode, tuple[ZKFeature, ...]]:
    """Resolve the three axes from CLI options, falling back to env vars."""
    version = pytestconfig.getoption("--zk-version") or os.environ.get(
        "ZK_VERSION", ZK_DEFAULT_VERSION
    )
    auth = ZKAuthMode(
        pytestconfig.getoption("--zk-auth")
        or os.environ.get("ZK_AUTH", ZKAuthMode.PLAIN.value)
    )
    features = tuple(
        ZKFeature(f.strip())
        for f in (
            pytestconfig.getoption("--zk-features")
            or os.environ.get("ZK_FEATURES", ZKFeature.STANDARD.value)
        ).split(",")
        if f.strip()
    )
    # Make the resolved values available to docker-compose interpolation.
    os.environ["ZK_VERSION"] = version
    os.environ["ZK_AUTH"] = auth.value
    os.environ["ZK_FEATURES"] = ",".join(f.value for f in features)
    os.environ["ZK_AUTH_JVMFLAGS"] = AUTH_JVM_FLAGS.get(auth, "")
    # Capture keylog agent flag (R-02): injected into SERVER_JVMFLAGS only
    # when capture is active on the tls flavor. The -javaagent path is
    # identical for every node; each JVM's /logs mount is per-node, so a
    # shared path yields per-node host keylog files (zk1|zk2|zk3). Always
    # export the variable so the base-file interpolation resolves cleanly.
    if ZKFeature.CAPTURE in features and auth is ZKAuthMode.TLS:
        capture_jvmflags = (
            "-javaagent:/agent/extract-tls-secrets.jar=/logs/tls-secrets.log"
        )
    else:
        capture_jvmflags = ""
    os.environ["ZK_CAPTURE_JVMFLAGS"] = capture_jvmflags
    return version, auth, features


@pytest.fixture(scope="session", autouse=True)
def docker_env(
    pytestconfig: pytest.Config,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[KazooZkEnv]:
    tmp_path: pathlib.Path = tmp_path_factory.getbasetemp()
    # Compose interpolates ${ZK_WORK_DIR} into bind-mount sources. Host-side
    # file ops below keep the native Path; the env var is what compose hands
    # to the daemon, so it may need translation to a daemon-visible mount
    # path on Windows-remotes (see _daemon_mount_path) (FR-011).
    os.environ["ZK_WORK_DIR"] = _daemon_mount_path(tmp_path)
    # Unique per-session compose project name keeps parallel test runs (and
    # any stray stacks from other projects) isolated from each other.
    os.environ["COMPOSE_PROJECT_NAME"] = f"kazoo-{uuid.uuid4().hex[:8]}"
    # Failure-injection tests (FR-009) stop and restart individual ensemble
    # members via `docker compose stop`/`start`. Docker re-randomizes
    # ephemeral host port mappings (`0:2181`) every time a container is
    # restarted, which would silently break those tests: the client keeps
    # reconnecting to the previously resolved host:port and gets
    # Connection refused. Publish each node on a *fixed* per-session host
    # port (allocated from a private range) so the mapping survives
    # restarts, while the random base keeps simultaneous runs isolated.
    _port_base = 22300 + (uuid.uuid4().int % 500) * 6
    for i, name in enumerate(("zoo1", "zoo2", "zoo3")):
        os.environ[f"{name.upper()}_CLIENT_PORT"] = str(_port_base + i * 3)
        os.environ[f"{name.upper()}_SECURE_PORT"] = str(_port_base + i * 3 + 1)
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
    docker_env: KazooZkEnv,
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

    # compose files live next to the integration tests. Locate the directory
    # via importlib.resources so discovery does not depend on __file__ (it
    # resolves to the real on-disk dir for any filesystem-backed install).
    context_path = pathlib.Path(resources.files("kazoo.tests") / "integ")
    context = str(context_path)
    # Relative bind-mount sources in the compose overlays (./jaas/...) are
    # interpolated through ${ZK_COMPOSE_DIR} so they can be translated to a
    # daemon-visible mount path on Windows-remote setups, exactly like
    # ${ZK_WORK_DIR} above (FR-011).
    os.environ["ZK_COMPOSE_DIR"] = _daemon_mount_path(context_path)
    _ensure_docker_available(context)

    compose = DockerCompose(
        context=context,
        compose_file_name=docker_compose_config["compose_files"],
    )

    # Capture preflight (R-07): when `capture` is active, build the in-repo
    # image declared by the capture overlay (dockerfiles/capture) *before*
    # `up`, so a build failure aborts the session with an actionable message
    # instead of failing opaquely mid-`up` (a network/registry outage for
    # `apk` tshark is reported here).
    if ZKFeature.CAPTURE in docker_compose_config["features"]:
        _build_capture_images(compose, context)

    global _COMPOSE_HANDLE
    try:
        compose.start()
        _COMPOSE_HANDLE = compose
        # Belt-and-suspenders beyond `up --wait`: fail fast with a precise
        # message if any ensemble member's ZK JVM is not actually healthy.
        # The healthcheck lives on the -service services (the netns holders
        # zoo1/zoo2/zoo3 run no JVM and carry no healthcheck).
        for node in ("zoo1-service", "zoo2-service", "zoo3-service"):
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
        # Assemble the TLS keylog + context certs (R-02/R-09) before the stack
        # goes down, so the decryption material for the pcapng artifacts is
        # available after teardown. No-op on non-tls/non-capture runs.
        emitted = _assemble_tls_keylog(
            docker_env.workdir, docker_env.auth, docker_env.features
        )
        if emitted:
            paths = ", ".join(map(str, emitted))
            print(f"[kazoo] capture keylog artifacts: {paths}")
        # Teardown never deletes capture artifacts (FR-009, R-05): `down
        # --volumes` removes only *named compose volumes* (the tmpfs zooN data
        # volumes), never the bound directories under ${ZK_WORK_DIR}
        # (captures/, logs/, certs/, agent/), so the pcapngs + decryption
        # material survive unchanged and remain on disk after the session for
        # analysis (quickstart V1/V2). See contracts/artifacts.md.
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

    # TLS-transport axes (tls, sasl_gssapi) expose the client port only on the
    # secureClientPort (2281, published as an ephemeral host port); plain,
    # digest and sasl_digest talk to the plain client port (2181).
    client_port = (
        2281
        if docker_env.auth in (ZKAuthMode.TLS, ZKAuthMode.SASL_GSSAPI)
        else 2181
    )

    # The ensemble exposes its client ports on ephemeral host ports; resolve
    # the actual host address/ports via the running compose stack.
    zk1_port = docker_compose.get_service_port("zoo1", client_port)
    zk2_port = docker_compose.get_service_port("zoo2", client_port)
    zk3_port = docker_compose.get_service_port("zoo3", client_port)

    if docker_env.auth is ZKAuthMode.SASL_GSSAPI:
        _export_krb5_client_env(docker_env, docker_compose)

    # ``get_service_host`` returns the publisher's bind address (``0.0.0.0`` /
    # ``::`` on macOS/Linux; testcontainers only rewrites those to 127.0.0.1 on
    # Windows). Clients must connect over the loopback interface where the
    # published ports actually listen, and the GSSAPI service principal for
    # sasl_gssapi is derived from the connect host (``zookeeper@<host>``), so a
    # wildcard host there yields ``zookeeper@0.0.0.0`` and a PROCESS_TGS error.
    zk_ip = docker_compose.get_service_host("zoo1", client_port)
    if zk_ip in ("0.0.0.0", "::", "::1", "localhost"):
        zk_ip = "127.0.0.1"

    return ZkEnsemble(
        zk_ip=zk_ip,
        zk1_port=zk1_port,
        zk2_port=zk2_port,
        zk3_port=zk3_port,
        version=docker_env.version,
        workdir=docker_env.workdir,
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
    """Create a KazooClient connected as superadmin to the ensemble."""
    chroot = (
        f"/{os.path.basename(request.node.nodeid)}-"
        f"{uuid.uuid4().hex[:8]}-superadmin"
    )
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
            f"Skipped: Zookeeper ensemble version {zkversion} matches "
            f"'{specifier}'"
        )
