# Data Model: Testing Resources Rationalization

**Feature**: [spec.md](spec.md) | **Date**: 2026-08-18

This refactor is a re-layout of test-support code and data; the entities below are the modules, resources, and pure-function contracts the plan must preserve or introduce. No runtime data store is involved.

## Module layout (the primary structure)

| Entity | Location | Contents | Lifecycle |
|--------|----------|----------|-----------|
| `kazoo.testing.common` | `kazoo/testing/common.py` | All business logic (enums, mappings, data classes, Docker/mount/keylog/Kerberos/axis/marker helpers) | New module; logic moved verbatim from `kazoo_ensemble.py`, with small pure-core extractions (R-05). |
| `kazoo.testing.fixtures` | `kazoo/testing/fixtures.py` | Thin pytest fixtures + plugin hooks delegating to `common`; extensive documentation | New module; fixture/hook bodies moved from `kazoo_ensemble.py` + `integ/conftest.py` (`docker_compose_config`). |
| `kazoo.testing.kazoo_ensemble` | `kazoo/testing/kazoo_ensemble.py` | — | Deleted (SC-006). |
| `kazoo.testing.__init__` | `kazoo/testing/__init__.py` | Package docstring + re-exports | Rewritten: history-free docstring; re-exports `common` and `fixtures`. |
| `kazoo/tests/conftest.py` | — | — | Deleted (FR-005); fully commented-out, no active hooks. |
| `kazoo/tests/integ/conftest.py` | unchanged location | Re-export of fixtures/hooks from `kazoo.testing.fixtures` | Import source changes; no behavioral change (FR-007). |

### Public surface preserved through the split (FR-007)

- Fixtures: `docker_env`, `docker_compose`, `zkensemble`, `zkchroot`, `zkclient`, `zksuperadmin_client`, `check_skip_version_marker`, `docker_compose_config`.
- Hooks: `pytest_addoption`, `pytest_configure`, `pytest_collection_modifyitems`, `pytest_sessionfinish`.
- Test-side imports that move: `test_capture.py` imports `ZKFeature`, `_assemble_tls_keylog`, `_evaluate_axis_markers` → now from `kazoo.testing.common`.

## Relocated resources (data files owned by the framework)

| Resource | New location | Referenced by | Invariant |
|----------|--------------|---------------|-----------|
| `docker-compose.base.yml` | `kazoo/testing/` | `resolve_compose_files` (base of every stack) | `build:`/`${ZK_COMPOSE_DIR}` relative refs resolve from its own directory; LF endings. |
| `docker-compose.auth-digest.yml`, `auth-sasl-digest.yml`, `auth-sasl-gssapi.yml`, `auth-tls.yml` | `kazoo/testing/` | `resolve_compose_files(auth)` overlay selection | Filenames unchanged (underscore→hyphen mapping preserved); `./dockerfiles/...` builds + `${ZK_COMPOSE_DIR}/jaas/...` mounts valid. |
| `docker-compose.features-capture.yml` | `kazoo/testing/` | `resolve_compose_files(..., features)` when `capture` active; `test_capture._CAPTURE_OVERLAY` | Filename unchanged; `./dockerfiles/capture` + `./dockerfiles/tls-secrets-agent` builds valid. |
| `jaas/sasl-digest.conf`, `jaas/sasl-gssapi.conf` | `kazoo/testing/jaas/` | `${ZK_COMPOSE_DIR}/jaas/...` mounts in overlays | Mounted read-only at `/conf/jaas.conf`; LF endings. |
| `dockerfiles/capture/`, `certgen/`, `kdc/`, `tls-secrets-agent/` | `kazoo/testing/dockerfiles/` | `build: ./dockerfiles/...` in overlays | Each tree keeps its Dockerfile + `*.sh` entrypoint; LF endings. |

### Path invariants (from `contracts/modules.md`)

- `context = resources.files("kazoo.testing")` — the compose context is the package root.
- `ZK_COMPOSE_DIR = _daemon_mount_path(context)` — daemon-visible path for `${ZK_COMPOSE_DIR}/jaas/...` (Windows-remote included).
- `ZK_WORK_DIR = _daemon_mount_path(session basetemp)` — unchanged behavior.

## Pure-function contracts (unit-test targets in `common.py`)

| Function | Signature (conceptual) | Output/behavior |
|----------|------------------------|-----------------|
| `resolve_axis_options(version_opt, auth_opt, features_opt, environ)` | options + env → `(version, auth, features, env_updates)` | Env fallbacks (`ZK_VERSION`→`3.9.5`, `ZK_AUTH`→`plain`, `ZK_FEATURES`→`standard`); coerces auth to `ZKAuthMode`; `env_updates` includes `ZK_CAPTURE_JVMFLAGS` only for `tls`+`capture`. |
| `_resolve_axis_options(pytestconfig)` | thin wrapper | Reads `getoption`/`os.environ`, applies `env_updates`, returns the triple. |
| `_evaluate_axis_markers(item, version, auth, features)` | → `str \| None` | Skip reason join, or `None`. |
| `evaluate_skip_version_marker(condition, active_version)` | → `bool` | `SpecifierSet` membership. |
| `resolve_compose_files(auth, features)` | → `list[str]` | `[base]` + optional auth overlay + optional capture overlay. |
| `_daemon_mount_path(path, os_name, docker_host)` | → `str` | `/mnt/<drive>` rewrite for Windows+tcp; passthrough otherwise. |
| `_process_service(name)` | → `str` | `zoo{1,2,3}` → `zooN-service`; else unchanged. |
| `ZkEnsemble.get_hosts()` | → `str` | `ip:p1,ip:p2,ip:p3`. |
| `ZkEnsemble._client_implied_options()` | → `dict` | Per-auth kwargs (digest `auth_data`, sasl `sasl_options`, tls/gssapi `use_ssl`+certs). |
| `_apply_superadmin_auth(kwargs)` | mutates kwargs | Appends/derives superadmin digest; `ValueError` on non-list existing `auth_data`. |
| `_assemble_tls_keylog(workdir, auth, features)` | → `list[Path] \| None` | Assembles `captures/tls/zk-secrets.log` + cert copies; `None` when no keylog material. |
| `probe_readable_captures(workdir)` | → `list[str]` | Interrupted-run artifact probe (SHB magic, both endiannesses). |
| `_write_host_krb5_conf(workdir, kdc_host, kdc_port)` | → `Path` | Host-view `krb5.client.conf`; `127.0.0.1` fallback for wildcard binds. |
| `set_compose_handle(compose)` / `dump_ensemble_logs()` | — | Shared-stack log dump (compose-bound). |

## Validation rules (derived from requirements)

- No pure function in `common.py` may import pytest or touch Docker at call time (FR-010/FR-006); Docker/subprocess-bound helpers are isolated from pure cores.
- `ZKFeature.CAPTURE` must stay out of `FEATURE_JVM_PROPERTIES` (capture contributes no server JVM flags).
- Marker registration names, skip reasons, and collection-time decisions byte-identical (FR-011).
- `KazooZkEnv`/`ZkEnsemble` frozen attrs fields unchanged (edge case: public-ish API used by integration tests).

## State transitions

N/A — no persistent state. Lifecycle is repository-edit-time: old paths removed, new paths created, references rewired, verified by the gates in `quickstart.md`.
