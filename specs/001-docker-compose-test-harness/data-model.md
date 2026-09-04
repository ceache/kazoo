# Data Model: Docker-Compose Test Harness

**Date**: 2026-08-14
**Spec**: [spec.md](./spec.md) · **Research**: [research.md](./research.md)

The harness is pytest infrastructure; the "data" below is the configuration and
runtime state the harness manages. It is implemented as `attrs` frozen
dataclasses plus pytest fixtures in `kazoo/testing/kazoo_ensemble.py` and
`kazoo/tests/integ/conftest.py`.

---

## Entity: Test Run Configuration

The resolved triple selected for one pytest invocation.

| Field | Type | Description | Validation |
|---|---|---|---|
| `version` | str | ZooKeeper image version tag (e.g. `3.9.4`) | One of the supported 3.7 / 3.8 / 3.9 series (FR-003); default `3.9.4` |
| `auth` | enum[str] | Auth scheme | One of `plain`, `digest`, `sasl_digest`, `sasl_gssapi`, `tls` (FR-004); default `plain` |
| `features` | set[str] | Feature toggles | Subset of `standard`, `ttl`, `readonly`, `reconfig` (FR-005); default `{standard}` |
| `workdir` | pathlib.Path | Session temp dir hosting logs / certs / keytabs | Set by the `docker_env` fixture from `tmp_path_factory`; exported to compose as `ZK_WORK_DIR` |

- **Source**: CLI flags `--zk-version` / `--zk-auth` / `--zk-features`, falling
  back to `ZK_VERSION` / `ZK_AUTH` / `ZK_FEATURES` env vars.
- **Relationship**: drives the `Compose Stack` selection (1:1) and the
  `Test Constraint Marker` evaluation (1:N).
- **Lifecycle**: resolved once per session by the `docker_env` fixture; immutable
  for the whole run.

## Entity: ZooKeeper Ensemble

The fixed 3-node cluster provisioned for a session (FR-016).

| Field | Type | Description |
|---|---|---|
| `nodes` | list[str] | Container service names `zoo1`, `zoo2`, `zoo3` |
| `host` | str | Host address (`compose.get_service_host(service, port)`) |
| `client_ports` | dict[str, int] | Host port mapped per node via `compose.get_service_port(service, 2181)` (ephemeral `0:2181` bind) |
| `secure_client_ports` | dict[str, int] | Host port for the TLS `secureClientPort` (auth flavors `tls`/`sasl_gssapi`) |
| `compose` | DockerCompose | testcontainers handle for `start`/`stop`/`logs` and per-node `stop`/`start` |

- **Behavior**: exposes `get_hosts()` (comma-joined `host:port` list) and
  `get_client(...)` (auth-aware KazooClient factory, see
  [contracts/client-connection.md](./contracts/client-connection.md)).
- **State transitions**: `provisioning → healthy (4LW ruok) → serving → stopped
  (session teardown)`; per-node `running ↔ stopped` via `zkensemble.stop("zooN")`
  / `start("zooN")` for failure-injection (FR-009); the harness waits for quorum
  re-establishment after restarts.

## Entity: Compose Stack

The materialized compose definition for the active configuration.

| Field | Type | Description |
|---|---|---|
| `files` | list[str] | Active compose file list: `docker-compose.base.yml` + (`docker-compose.auth-<mode>.yml` when auth != plain) |
| `project_name` | str | Unique session project name (`kazoo-<uuid8>`) via `COMPOSE_PROJECT_NAME` |
| `interpolation_vars` | dict[str, str] | `ZK_VERSION`, `ZK_WORK_DIR`, `ZK_AUTH`, `ZK_FEATURES`, `ZK_FEATURES_JVMFLAGS`, `ZK_AUTH_JVMFLAGS`, `COMPOSE_PROJECT_NAME` exported to the compose process |
| `auth` | str | Active auth flavor (selects the auth overlay) |
| `features` | set[str] | Rendered into `SERVER_JVMFLAGS` via `ZK_FEATURES_JVMFLAGS` |

- **Selection**: by the `docker_compose_config` fixture in
  `kazoo/tests/integ/conftest.py` (base + auth overlay list; `docker compose
  config` validates the merged definition).
- **Relationship**: 1:1 with `Test Run Configuration`; owns the `ZooKeeper
  Ensemble` (1:1) and the auth sidecars (KDC, certgen).

## Entity: Test Namespace (Chroot)

Per-test isolation namespace on the shared ensemble (FR-007).

| Field | Type | Description |
|---|---|---|
| `path` | str | Unique root path, e.g. `/test_<nodeid>-<uuid8>` |
| `owner_test` | str | The pytest node id that created it |

- **Lifecycle**: created by `zkclient`/`zksuperadmin_client` fixtures at setup
  (`ensure_path`), removed recursively at teardown (SC-006); survives client
  reconnects but not ensemble restarts (session-scoped only).
- **Relationship**: many namespaces per ensemble; one per test function.

## Entity: Test Constraint Marker

Declarative test annotation evaluated against the active configuration.

| Field | Type | Description |
|---|---|---|
| `min_version` / `max_version` | str | Version bounds (e.g. `<3.8`, `>=3.9`) |
| `auth` | str \| list[str] | Required auth scheme(s) |
| `require_features` | list[str] | Features the test needs (skip if missing) |
| `skip_features` | list[str] | Features incompatible with the test (skip if present) |

- **Evaluation**: at collection time (`pytest_collection_modifyitems`) and/or by
  the `check_skip_version_marker` autouse fixture against the active
  `Test Run Configuration`; mismatches produce an explicit skip reason (FR-008,
  SC-005).
- **Current implementation**: `@pytest.mark.skip_if_zk_version("<3.5")`
  (SpecifierSet-based, already live in `kazoo_ensemble.py`); the richer
  `zk_version` / `zk_auth` / `zk_features` markers from PYTEST_INTEG.md are the
  target surface (see [contracts/markers.md](./contracts/markers.md)).

---

## Relationships (summary)

```
Test Run Configuration ──1:1──▶ Compose Stack ──1:1──▶ ZooKeeper Ensemble
        │                                                  │
        │ 1:N (evaluated by)                               │ 1:N
        ▼                                                  ▼
Test Constraint Markers                            Test Namespaces (per test)
```

- `Test Run Configuration` → `Compose Stack`: selecting auth/features picks the
  compose file list and interpolation variables.
- `ZooKeeper Ensemble` → `Test Namespace`: many ephemeral namespaces per
  ensemble, one per running test.
- `Test Run Configuration` vs `Test Constraint Marker`: skip decisions at
  collection/setup.
- Auth sidecars (KDC, certgen) belong to the `Compose Stack` and feed
  `ZK_WORK_DIR` (keytabs, certs, krb5.conf) consumed by host-side clients.
