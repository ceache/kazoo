# Research: Docker-Compose Test Harness

**Date**: 2026-08-14
**Feature**: [spec.md](./spec.md)
**Phase**: 0 (unknowns resolution for the `/speckit.plan` design)

This document resolves every "NEEDS CLARIFICATION" and open decision in the
feature spec against the current branch state (in-progress `kazoo/testing/kazoo_ensemble.py`
and `kazoo/tests/integ/`). See `docs/testing.rst` for the architecture/technical
implementation of the composite harness, which supersedes the earlier
`PYTEST_INTEG.md` and `COMPOSE-STRATEGY.md` strategy docs.

---

## R-01: Docker Compose orchestration driver

- **Decision**: Use **testcontainers-python** (`testcontainers.compose.DockerCompose`,
  **4.x**) as the compose orchestration driver in the harness (user-directed
  change; supersedes the pytest-docker baseline in the in-progress
  implementation).
- **Verified API surface** (testcontainers 4.13.3 wheel source, Requires-Python
  `>=3.9.2`):
  - `DockerCompose(context, compose_file_name: str | list[str], pull, build,
    wait=True, keep_volumes, env_file, services, docker_command_path, profiles)` —
    **list of compose files** enables the COMPOSE-STRATEGY overlay model (R-09).
  - `start()` runs `docker compose up --wait` (wait=True) — readiness is
    **healthcheck-driven** via the compose healthchecks we already define; with
    `wait=False` it runs `up --detach`.
  - `stop(down=True)` runs `docker compose down --volumes` (clean teardown);
    `stop(down=False)` runs `docker compose stop`.
  - `get_service_host(service, port)` / `get_service_port(service, port)` /
    `get_service_host_and_port(service, port)` resolve host + **ephemeral**
    bound ports via `docker compose ps --format json` (works with `0:2181`).
  - `get_logs(*services)` returns `(stdout, stderr)` per service (FR-015).
  - `get_container(service)` exposes per-container `State`/`Health`.
  - `exec_in_container(command, service)` for in-container commands.
  - `compose_command_property` (property) returns the base `docker compose -f ...`
    command list (public) — used for per-node `stop`/`start` (FR-009) and `config`
    validation.
- **Rationale**: matches the multiplatform mandate (drives the modern `docker
  compose` v2 CLI, no legacy `docker-compose` v1 binary); replaces the
  pytest-docker `docker_services`/`docker_ip`/`port_for` fixtures with a
  self-owned session fixture wrapping `DockerCompose`; multi-file support
  unlocks the overlay layout. Python >= 3.9.2 is required → Python 3.8 is
  dropped from the test support matrix (clarification, plan refinement).
- **Alternatives considered**:
  - *pytest-docker (current in-progress baseline)*: rejected by the user
    directive; also its `docker_compose_file` fixture accepts a single compose
    path, forcing the per-auth single-file layout (R-09 alternative).
  - *testcontainers 3.7.1 (<4)*: the only 3.x that supports Python 3.8, but it
    hardcodes the **legacy `docker-compose` v1 binary**, has no healthcheck
    waiting, no per-service logs, and no container state — breaks the
    multiplatform mandate. Rejected.
  - *Raw `subprocess` around the `docker compose` CLI*: hand-rolled lifecycle and
    port discovery — exactly the kind of hand-rolled code this feature retires.

---

## R-02: Official `zookeeper` image configuration interfaces

- **Decision**: Configure the ensemble **exclusively** through the official image's
  environment-variable interfaces; never write `zoo.cfg` / log4j files and never
  launch Java directly (FR-002).
- **Rationale**: The official image entrypoint (`docker-entrypoint.sh`) translates
  `ZOO_*` env vars into the ZooKeeper config, uniformly across 3.7–3.9:
  - `ZOO_MY_ID`, `ZOO_SERVERS` (e.g. `server.1=0.0.0.0:2888:3888;2181 ...`)
  - `ZOO_CFG_EXTRA` (appended verbatim to `zoo.cfg`: auth providers, secure ports,
    readonly mode, etc.)
  - `ZOO_4LW_COMMANDS_WHITELIST`, `ZOO_ADMINSERVER_ENABLED`, `ZOO_STANDALONE_ENABLED`
  - JVM flags: `JVMFLAGS` (classic) and `SERVER_JVMFLAGS` (3.8+ logback era);
    the current `docker-compose.yml` already sets both `SERVER_JVMFLAGS` and
    `ZOO_LOG4J_PROP`/`ZOO_LOG_DIR` to span 3.7–3.9.
- **Healthcheck (verified)**: The official image (Ubuntu 22.04 base) ships both
  `/usr/bin/nc` and `/usr/bin/bash`. Therefore the 4-letter-word healthcheck
  `echo ruok | nc -w 2 127.0.0.1 2181 | grep -q imok` (and `srvr` variant) runs
  in-container without extra tooling. Verified locally against `zookeeper:3.9.4`.
- **Image tags**: exact patch tags for reproducibility — defaults `3.9.4`, with
  3.7.x / 3.8.x pinned per CI matrix (e.g. 3.7.2, 3.8.3 matching current CI pins).
- **Alternatives considered**: `curl`-based TCP checks (not guaranteed present),
  `dockerize -wait` (extra tool), bash `/dev/tcp` (works but no 4LW response
  verification). The `nc` + 4LW approach is simplest and verified.

---

## R-03: Auth matrix server-side configuration per flavor

Each auth flavor is materialized as a compose overlay on the base definition
(see R-09), all configuring the official image via env vars.

- **plain**: default `clientPort` 2181; no auth provider; no JVM auth flags.
- **digest**: `SERVER_JVMFLAGS` includes
  `-Dzookeeper.DigestAuthenticationProvider.superDigest="super:..."`; client
  connects with `auth_data=[("digest", "super:super_secret")]` (already implemented).
- **sasl_digest**: JAAS `Server { DigestLoginModule ... }` mounted at
  `/conf/jaas.conf`; `JVMFLAGS=-Djava.security.auth.login.config=/conf/jaas.conf`;
  `ZOO_CFG_EXTRA` sets
  `authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider` +
  `enforce.auth.enabled=true` + `enforce.auth.schemes=sasl`. Client uses
  `sasl_options={mechanism: "DIGEST-MD5"}` (via `pure_sasl`).
  > **Verified on ZK 3.9.4**: the legacy `requireClientAuthScheme=sasl` property
  > is NOT recognized by ZK 3.7+ (never enforced; ZOOKEEPER-2668) and
  > `authProvider.1=SASLAuthenticationProvider` fails with
  > `ClassNotFoundException` because the value is used as a class name. The
  > modern enforcement keys are `enforce.auth.enabled` + `enforce.auth.schemes`
  > (constants `zookeeper.enforce.auth.enabled`/`zookeeper.enforce.auth.schemes`
  > confirmed in `AuthenticationHelper`) or the shorthand
  > `sessionRequireClientSASLAuth=true`. With enforcement on, an invalid
  > DIGEST-MD5 client raises `SessionClosedRequireSaslError` (server closes the
  > session before it is usable).
- **sasl_gssapi (with TLS tunnel per FR-012)**: combines the KDC sidecar
  (R-04), the JAAS `Krb5LoginModule` server config, **and** the TLS transport
  (R-05): `secureClientPort`, `serverCnxnFactory=NettyServerCnxnFactory`,
  `ssl.*`, `authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider`,
  `enforce.auth.enabled=true`, `enforce.auth.schemes=sasl`. Client connects with
  BOTH `use_ssl=True`
  (client cert + CA) and `sasl_options={mechanism: "GSSAPI"}` — i.e. GSSAPI
  authentication happens inside the TLS-validated tunnel. This is the combined
  mode the user explicitly called out and is required by FR-012.
- **tls**: `secureClientPort`, Netty factory, `ssl.keyStore/trustStore` (PKCS12),
  `ssl.clientAuth=need`, `authProvider.1=X509AuthenticationProvider`. Client uses
  `use_ssl=True` with `certfile`/`keyfile`/`ca` pointing at the certgen output
  (R-05).
- **Combined matrix note**: ZooKeeper supports TLS (transport) + SASL (auth)
  simultaneously; the secure port carries the SASL handshake. Server needs the
  SASL auth provider (GSSAPI), TLS config, and both 4LW whitelist entries.

---

## R-04: Kerberos KDC — in-repo Alpine build

- **Decision**: Build the KDC in-repo as an **Alpine** image committed at
  `kazoo/tests/integ/dockerfiles/kdc/` (FR-018). No third-party KDC image.
- **Rationale**: the KDC Dockerfile (Alpine-based) installs
  `krb5-admin-server krb5-kdc`, runs as user `daemon`, and its `entrypoint.sh`:
  1. writes `krb5.conf` (realm `EXAMPLE.ORG`, KDC listening on `127.0.0.1:1088`),
  2. creates the principal database (`kdb5_util create -s`),
  3. adds principals from the `SPNS` env var (e.g. `client server/zoo1
     server/zoo2 server/zoo3`) and exports their keytabs to
     `/kdc-data/keytabs` with `/`→`#` in filenames (`server#zoo1.keytab`),
  4. makes keytabs world-readable, then starts `krb5kdc -n`.
  This matches the legacy `init_krb5.sh` behavior the old GSSAPI tests relied on.
- **Alpine port**: `apk add krb5-server krb5` provides `krb5kdc`, `kadmind`,
  `kdb5_util`, `kadmin.local`. Alpine's default shell is busybox `ash`, not
  `bash`; the entrypoint must be rewritten in POSIX `sh` (the script is simple
  enough) or `apk add bash`. Decision: rewrite in POSIX sh to keep the image lean.
- **Host access to keytabs**: the KDC container exports keytabs to a volume that
  must be readable by the host-side pytest process (the Kazoo client needs its
  own keytab + `krb5.conf`). Bind-mount the pytest session work dir
  (`ZK_WORK_DIR`, from `tmp_path_factory`) into the KDC at `/kdc-data`; the
  GSSAPI client on the host reads `ZK_WORK_DIR/keytabs/client.keytab` and
  `ZK_WORK_DIR/krb5.conf` (with `KRB5_CONFIG` pointed at it).
- **SPNS**: `client server/zoo1 server/zoo2 server/zoo3` (already referenced in
  the repo's root compose comments); realm default `EXAMPLE.ORG`.
- **Alternatives considered**: third-party image `gcavalcante8808/krb5-kdc`
  (COMPOSE-STRATEGY) — rejected: trust root outside repo control, FR-018 forbids.

---

## R-05: TLS certificate generation — ephemeral certgen sidecar

- **Decision**: Generate throwaway CA + server PKCS12 keystore/truststore +
  client PEM certs inside an ephemeral `certgen` sidecar container
  (`eclipse-temurin:17-jdk-jammy` + `openssl` + `keytool`), writing to the
  shared `ZK_WORK_DIR/certs` bind mount (host-accessible), then Zookeeper reads
  them from the same volume. Mirrors `COMPOSE-STRATEGY.md § mTLS`.
- **Rationale**: Replaces the in-process `pyOpenSSL` + `jks` certificate
  generation in `kazoo/testing/common.py` (removed by FR-010/SC-003). All
  certificates are throwaway test values (FR-013). Using a bind mount from the
  pytest session temp dir gives the host client access to client cert/key/CA
  with no platform-specific path handling (Windows/macOS/Linux temp dirs work).
- **Alternatives considered**: host-side `openssl`/`keytool` invocations
  (requires Java/openssl on the dev machine — breaks multiplatform goal),
  pre-committed certs (forbidden by FR-013 / security policy).

---

## R-06: Client connection mapping per auth axis

- **Decision**: Extend `ZkEnsemble.get_client()` (in `kazoo_ensemble.py`) so the
  active auth axis implies the correct KazooClient options, unless the caller
  overrides them explicitly:
  - plain → no extra options
  - digest → `auth_data=[("digest", "super:super_secret")]`
  - sasl_digest → `sasl_options={"mechanism": "DIGEST-MD5"}`
  - sasl_gssapi → `use_ssl=True` + `certfile/keyfile/ca` from `ZK_WORK_DIR/certs`
    + `sasl_options={"mechanism": "GSSAPI"}` + `KRB5_CONFIG` pointing at the
    shared `krb5.conf` and client keytab path in `KRB5_CLIENT_KTNAME`
  - tls → `use_ssl=True` + `certfile/keyfile/ca` from `ZK_WORK_DIR/certs`
- **Rationale**: keeps test code agnostic to the auth axis; the existing
  implementation already does most of this (including `superadmin` digest
  augmentation). The change needed: `sasl_gssapi` now also implies the TLS
  tunnel options per FR-012.
- **Superadmin**: `zksuperadmin_client` / `get_client(superadmin=True)` still
  adds the `("digest", "super:super_secret")` auth data; works on the `digest`
  flavor (superDigest JVM flag) and on plain/digest base configs.

**Verified 2026-08-15 (sasl_gssapi end-to-end on ZK 3.9.4, macOS + Docker
Desktop)** — three host-side/client-side requirements surfaced:

1. **KDC transport must be forced to TCP.** The overlay publishes
   `0:1088` + `0:1088/udp`; on macOS, Docker Desktop's userland proxy
   silently drops the KDC's UDP replies, so a host-view `kdc = host:port`
   entry leaves Heimdal stuck (`unable to reach any KDC`). The host-view
   `krb5.conf` must write `kdc = tcp/<host>:<port>`.
2. **The client needs a fresh FILE credential cache per run.** macOS defaults
   to the shared `API:...` ccache, which may still hold a TGT + service ticket
   (`zookeeper/127.0.0.1@EXAMPLE.ORG`) minted by a *previous* KDC instance
   (each compose stack runs its own realm with new keys). Reusing that stale
   ticket makes the fresh server fail with `GSS initiate failed` / `Checksum
   failed`. Heimdal `kinit` ignores `KRB5CCNAME=FILE:...` from the
   environment, so the harness must create the cache explicitly via
   `kinit -c <file> -kt <client.keytab> client@EXAMPLE.ORG` and then export
   `KRB5CCNAME=FILE:<file>`.
3. **The ensemble host must be the loopback, not the publisher bind
   address.** `get_service_host` returns the container's bind address
   (`0.0.0.0` on macOS/Linux; testcontainers only rewrites it to
   `127.0.0.1` on Windows). The GSSAPI service principal is derived from the
   connect host (`zookeeper@<host>`), and the KDC only provisions
   `zookeeper/127.0.0.1` and `zookeeper/localhost` — so a client pointed at
   `0.0.0.0` requests `zookeeper@0.0.0.0` and the KDC fails with
   `PROCESS_TGS`. The `zkensemble` fixture normalizes wildcard bind hosts to
   `127.0.0.1`.

Getting all three right yields a passing GSSAPI-in-TLS positive test, plus a
negative test that rejects a nonexistent service (`nosuchsvc` → `PROCESS_TGS`
→ session unusable / `AuthFailedError`).

---

## R-07: Cluster logs on test failure

- **Decision**: Surface `compose.get_logs("zoo1", "zoo2", "zoo3", ...)` (plus
  sidecar services for the active flavor) when a test fails, via a pytest hook
  (`pytest_exception_interact` / `pytest_sessionfinish` on failure) that prints
  the `(stdout, stderr)` output to the test report/log (FR-015). `DockerCompose`
  also exposes per-service logs, so only the services relevant to the failure
  need dumping.
- **Rationale**: The legacy `conftest.py` had an equivalent (commented-out)
  `pytest_exception_interact` dumping cluster logs; the constitution requires
  cluster logs surfaced on failure. testcontainers' `get_logs(*services)` is a
  public, structured API (no shelling out to `docker compose logs` directly).
- **Alternatives considered**: log capture only in CI (loses local developer
  value), a pytest `--collect`-time dump (wrong timing).

---

## R-08: Legacy test migration mapping

Nine top-level test files still use the legacy API and MUST be migrated
(FR-010): `test_election`, `test_gevent_handler`, `test_interrupt`, `test_lease`,
`test_lock`, `test_partitioner`, `test_party`, `test_queue`, `test_sasl`.

| Legacy API | New harness equivalent |
|---|---|
| `class MyTests(KazooTestCase)` + `setUp`/`tearDown` | pytest class/function using `zkclient`, `zkensemble` fixtures |
| `self.client` | `zkclient` fixture |
| `self._get_client(**opts)` / `self.servers` | `zkensemble.get_client(**opts)` / `zkensemble.get_hosts()` |
| `self.cluster[i].stop()/start()` | `zkensemble.stop("zooN")` / `zkensemble.start("zooN")` |
| `self.lose_connection(event_factory)` / `self.expire_session(...)` | `zkensemble.lose_connection(client, ...)` / `client.harness_expire_session` |
| `self.secure_servers` / `get_ssl_client_configuration()` | `zkensemble.get_client(use_ssl=True, certfile=..., keyfile=..., ca=...)` from `ZK_WORK_DIR/certs` |
| `KRB5_TEST_ENV` / GSSAPI env (`init_krb5.sh`) | KDC sidecar + `KRB5_CONFIG` / `KRB5_CLIENT_KTNAME` from `ZK_WORK_DIR` |
| `@pytest.mark.skip_if_zk_version("<3.5")` | Keep marker; harness evaluates against the active version (already implemented) |
| handler-specific tests (gevent/eventlet) | `zkensemble.get_client(handler=...)` keeps working; `zkchroot`/`zkclient` are handler-agnostic |

- **Decision**: mechanical, coverage-preserving per-file migration; each file is
  verified by running it against the compose harness before the legacy modules
  (`kazoo/testing/harness.py`, `kazoo/testing/common.py`) and the public exports
  (`kazoo/testing/__init__.py`) are deleted. The removal is recorded under
  BREAKING CHANGES in `CHANGES.md` (constitution IV).
- **Risk**: `test_sasl.py` exercises server-side JAAS configurations; its three
  classes map to the `sasl_digest` and `sasl_gssapi` auth flavors. `test_lock` /
  `test_queue` etc. are large; keep their class structure, swap fixtures.

---

## R-09: Compose file layout & feature-flag mechanism

- **Decision**: **Layered overlay layout** (COMPOSE-STRATEGY.md), enabled by
  testcontainers' list-of-files support (R-01): one **base** definition plus one
  **auth overlay** per flavor, passed to `DockerCompose(compose_file_name=[
  "docker-compose.base.yml", "docker-compose.auth-<mode>.yml"])`. Feature
  toggles are rendered on the host via env-var interpolation — no feature
  overlay files.
- **JVM-flag composition (avoids overlay env-merge overrides)**: compose merges
  `environment` maps wholesale per key (last file wins), so `SERVER_JVMFLAGS`
  must be defined in **exactly one** file. The base file renders
  `SERVER_JVMFLAGS: ${ZK_FEATURES_JVMFLAGS} ${ZK_AUTH_JVMFLAGS}`; the conftest
  `docker_compose_config` fixture computes `ZK_AUTH_JVMFLAGS`
  (`-Dzookeeper.DigestAuthenticationProvider.superDigest="super:..."` for
  `digest`, empty otherwise) alongside the existing `ZK_FEATURES_JVMFLAGS`. Auth
  overlays therefore never set `SERVER_JVMFLAGS` (they set `JVMFLAGS` for the
  JAAS `-Djava.security.auth.login.config` and `ZOO_CFG_EXTRA`), eliminating the
  override hazard.
- **Layout**:
  ```
  kazoo/tests/integ/
  ├── conftest.py
  ├── docker-compose.base.yml                    # default (plain) 3-node ensemble; SERVER_JVMFLAGS interpolation
  ├── docker-compose.auth-digest.yml             # superDigest via ZK_AUTH_JVMFLAGS only (skeleton overlay)
  ├── docker-compose.auth-sasl-digest.yml        # JAAS DigestLoginModule + SASL provider
  ├── docker-compose.auth-sasl-gssapi.yml        # + kdc service (depends_on service_healthy) + JAAS Krb5 + TLS transport
  ├── docker-compose.auth-tls.yml                # + certgen service + Netty + secureClientPort + ssl.*
  └── dockerfiles/kdc/                           # Alpine KDC (in-repo, R-04)
      ├── Dockerfile
      └── root/entrypoint.sh
  ```
  File list = `[base]` (+ `[auth-<mode>]` when auth != plain). `docker compose
  config` validation uses the same `-f` list.
- **Session isolation**: each pytest session sets a unique `COMPOSE_PROJECT_NAME`
  (e.g. `kazoo-<uuid8>`) in the process environment before any compose command,
  so concurrent sessions (parallel CI jobs, local + CI) cannot collide on
  project/container names; `down --volumes` at teardown then targets exactly
  that project.
- **Sidecar ordering**: ZooKeeper services `depends_on` auth sidecars with
  `condition: service_healthy` (KDC healthcheck = keytab files present;
  certgen healthcheck = keystore files present). Compose `up --wait` honors
  healthchecks and dependency ordering.
- **Shared invariants across all flavors** (from the current `docker-compose.yml`):
  ephemeral host ports (`0:2181`), tmpfs `/data` + `/datalog`, host bind mount
  for logs (`${ZK_WORK_DIR}/logs`), 4LW whitelist, and the
  `ruok`/`srvr` healthcheck verified in R-02.
- **Alternative rejected**: per-auth self-contained single files
  (`docker-compose/<auth>/docker-compose.yml`), forced by pytest-docker's
  single-path `docker_compose_file` fixture; abandoned with the driver change
  because it duplicated the ensemble skeleton five times.

---

## R-10: Test dependency declarations

- **Decision**: Add **`testcontainers>=4.0,<5`** to `[options.extras_require]
  test` in `setup.cfg` (FR-014) — it is the orchestration driver and replaces
  pytest-docker. **Remove** `pytest-docker` (superseded), `pyjks` (its only
  consumer `kazoo/testing/common.py` is deleted), and `pyOpenSSL` (no remaining
  consumer after `common.py` removal). The harness environment now requires
  **Python >= 3.9** (testcontainers 4.x constraint); the Python 3.8 classifier
  and CI entries are dropped from the support matrix.
- **Rationale**: `kazoo_ensemble.py` will import `testcontainers.compose`; a
  fresh `pip install -e .[test]` must not fail to find the driver (currently
  pytest-docker is undeclared — a real gap). `pyjks`/`pyOpenSSL` were only
  needed for in-process cert generation (R-05).
- **Constraint note**: base deps (`packaging`, `attrs`) are already runtime
  requirements of kazoo; no change needed. testcontainers 4.x pulls in `docker`,
  `python-dotenv`, `typing_extensions`, `requests`, `wrapt` as transitive deps.

---

## R-11: Startup budget / performance (deferred planning knob)

- **Decision**: Session startup (provision + healthcheck) is bounded by the
  compose healthcheck settings: interval ~3s, timeout ~3s, retries 15–20
  (≈45–60s window, plus image-pull on first run). Per-test timeout stays 180s
  (existing `pyproject.toml`). The harness reuses one session cluster, so the
  cost is amortized across all tests (SC-001/SC-006 context).
- **Rationale**: matches COMPOSE-STRATEGY base healthcheck and the existing
  compose file; the exact retry counts are tuning knobs owned by `tasks.md`,
  not architecture.
- **Note**: on a warm Docker cache a 3-node ensemble forms quorum in ~10–30s.

---

## R-12: Multiplatform details

- **Decision**: all host-side logic is pure Python/pytest with `pathlib`;
  shell-out only to the **`docker compose` (v2) CLI via testcontainers**;
  container-side shell is bash/sh inside the official image / Alpine KDC. No
  host `java`, `keytool`, `openssl`, or `krb5` binaries are required (FR-011).
- **Compose v2 floor**: `docker compose up --wait` (healthcheck-driven
  readiness) and `docker compose ps --format json` (port discovery) require
  **Compose v2.12+** — satisfied by Docker Desktop (macOS/Windows) and
  current CI runners; documented as a prerequisite (FR-011 scope: any host with
  a docker-compose compatible CLI).
- **Windows**: Docker Desktop provides `docker compose`; ephemeral port mapping
  and bind mounts of the pytest temp dir work on Windows (paths are passed to
  compose as-is; testcontainers handles the URL normalization for `0.0.0.0`).
  `tmp_path_factory` gives a platform-safe host dir for `ZK_WORK_DIR`.
- **CI**: `ubuntu-latest` runners have compose v2 + Docker preinstalled; the
  tiered matrix (FR-017) drops the `zookeeper/` download cache, the
  `ensure-zookeeper-env.sh` step, apt-installed `krb5-*` packages, and the
  Python 3.8 runner entry (R-10).

---

## Consolidated unknowns → decisions

| Unknown / question | Resolution | Where |
|---|---|---|
| Orchestration driver | testcontainers 4.x `DockerCompose` (supersedes pytest-docker) | R-01 |
| Image config interface | `ZOO_*` env vars + JVMFLAGS/SERVER_JVMFLAGS only | R-02 |
| Healthcheck tooling | `nc` + 4LW, verified present in image | R-02 |
| Ensemble size | fixed 3-node (clarification Q1 / FR-016) | R-09 |
| GSSAPI + TLS combined mode | sasl_gssapi flavor = TLS tunnel + GSSAPI auth | R-03/R-05/R-06 |
| KDC image | in-repo Alpine build at `kazoo/tests/integ/dockerfiles/kdc/` | R-04 |
| TLS cert generation | ephemeral certgen sidecar → shared bind mount | R-05 |
| Legacy migration | 9 files, coverage-preserving, then delete legacy modules | R-08 |
| Dependency declarations | add testcontainers>=4,<5; drop pytest-docker, pyjks, pyOpenSSL | R-10 |
| Python floor | 3.9+ (testcontainers 4.x constraint; 3.8 dropped) | R-01/R-10 |
| CI matrix | tiered per PYTEST_INTEG (clarification Q2 / FR-017) | R-12 |
| Compose layout | base + auth overlays (COMPOSE-STRATEGY) via multi-file support | R-09 |
