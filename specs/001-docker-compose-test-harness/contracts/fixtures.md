# Contract: Pytest Fixtures

**Feature**: [Docker-Compose Test Harness](../spec.md)

Provided by `kazoo/testing/kazoo_ensemble.py` and re-exposed by
`kazoo/tests/integ/conftest.py`.

## Session-scoped

### `docker_env` → `KazooZkEnv` (autouse)

Resolves the three axes into an immutable environment descriptor.

| Attribute | Type | Description |
|---|---|---|
| `version` | str | ZK image tag |
| `auth` | str | auth flavor |
| `features` | tuple[str, ...] | active features |
| `workdir` | pathlib.Path | session temp dir (`ZK_WORK_DIR`) |

**Lifecycle**: runs for the whole session; sets `ZK_WORK_DIR`, exports
`COMPOSE_PROJECT_NAME` (session-unique), and prepares the interpolation vars
before any compose command.

### `docker_compose_config` → `dict`

Selects the active compose **file list** (base + auth overlay) for the auth
flavor and exports `ZK_VERSION`/`ZK_AUTH`/`ZK_FEATURES`/`ZK_FEATURES_JVMFLAGS`/
`ZK_AUTH_JVMFLAGS`/`COMPOSE_PROJECT_NAME` for compose interpolation and project
isolation. Returns `{version, auth, features, compose_files}`.

### `docker_compose` → `testcontainers.compose.DockerCompose` (session-scoped)

The orchestration handle; replaces pytest-docker's `docker_services`.

- **Lifecycle**: constructed with `context=kazoo/tests/integ` and
  `compose_file_name=docker_compose_config["compose_files"]`; `start()` (→
  `docker compose up --wait`, healthcheck-driven readiness) at session setup;
  `stop()` (→ `docker compose down --volumes`) at session teardown.
- **Contract**: provides `get_service_host(service, port)` /
  `get_service_port(service, port)` for ephemeral host-port resolution,
  `get_logs(*services)` for failure diagnostics (FR-015), and the
  `compose_command_property` base command for per-node control.

## Function-scoped

### `zkensemble` → `ZkEnsemble`

Handle on the running ensemble: `zk_ip`, `zk1_port`/`zk2_port`/`zk3_port`,
`get_hosts()`, `get_client(...)`, `stop("zooN")`, `start("zooN")`,
`lose_connection(client)`, `expire_session(client)`.

- Per-test instance; clients it creates are caller-managed.
- `stop`/`start` run `compose.compose_command_property + ["stop", "zooN"]` /
  `["start", "zooN"]` via a harness helper; callers that restart a node must
  wait for the client to reconnect (the existing request-queuing tests do this).

### `zkchroot` → `str`

Unique per-test chroot path (`/<nodeid>-<uuid8>`) to scope test data.

### `zkclient` → `KazooClient`

Connected, started client scoped to `zkchroot`. **Contract**:

- created via `zkensemble.get_client()` (auth-aware options applied);
- `client.chroot` is set to the unique namespace;
- teardown guarantees: `client.stop()` + `client.close()`;
- `client.harness_expire_session` is bound for session-expiry tests;
- the namespace is removed recursively during teardown (SC-006).

### `zksuperadmin_client` → `KazooClient`

Like `zkclient`, but authenticated as `super` (digest) via
`get_client(superadmin=True)`; its chroot is suffixed `-superadmin`.

## Test-author guarantees

1. Clients returned by fixtures are started and connected before the test body.
2. Fixtures are isolated per test; no state leaks across tests.
3. A test may stop/start ensemble nodes via `zkensemble`; fixtures remain usable.
4. On failure, the harness dumps ensemble logs (FR-015).

## Validation

- A test using `zkclient` runs against the shared session ensemble; multiple
  concurrent tests in one session share the cluster but never the namespace.
- Env/CLI mismatches surface as clear skip reasons, not connection errors
  (FR-008).

See also [markers.md](./markers.md) and [client-connection.md](./client-connection.md).
