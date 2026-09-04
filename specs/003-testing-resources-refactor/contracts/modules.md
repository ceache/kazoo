# Contract: `kazoo.testing` Module Surface & Resource Path Invariants

**Feature**: [spec.md](../spec.md) | **Date**: 2026-08-18

This contract fixes the public surface and path invariants the refactor must preserve so the integration suite and downstream consumers behave identically after the `kazoo_ensemble.py` split and the resource move.

## 1. Importable names

### From `kazoo.testing.fixtures` (pytest glue)

```python
from kazoo.testing.fixtures import (
    # fixtures
    docker_env, docker_compose, zkensemble, zkchroot, zkclient,
    zksuperadmin_client, check_skip_version_marker, docker_compose_config,
    # plugin hooks
    pytest_addoption, pytest_configure,
    pytest_collection_modifyitems, pytest_sessionfinish,
)
```

### From `kazoo.testing.common` (business logic)

```python
from kazoo.testing.common import (
    ZKAuthMode, ZKFeature, ZK_DEFAULT_VERSION,
    FEATURE_JVM_PROPERTIES, AUTH_JVM_FLAGS,
    KazooZkEnv, ZkEnsemble,
    # integration-test imports (moved from kazoo_ensemble)
    _assemble_tls_keylog, _evaluate_axis_markers,
)
```

`kazoo/tests/integ/conftest.py` re-exports the fixture/hook names above; `kazoo/tests/integ/test_capture.py` imports `ZKFeature`, `_assemble_tls_keylog`, `_evaluate_axis_markers` from `kazoo.testing.common`.

### Removed names (SC-006)

- `kazoo.testing.kazoo_ensemble` — deleted; every in-repo reference (conftest, test_capture, docs, mypy config, workflow comment) is updated in the same change.
- `kazoo.tests.conftest` — deleted (was fully commented out); the mypy override entry is dropped.

## 2. Resource path invariants

With the resources relocated into `kazoo.testing`:

| Symbol | Value | Requirement |
|--------|-------|-------------|
| compose context | `str(resources.files("kazoo.testing"))` | Context is the package root; resolvable from installed wheel/sdist. |
| `ZK_COMPOSE_DIR` | `_daemon_mount_path(context)` | `${ZK_COMPOSE_DIR}/jaas/...` mounts resolve to the relocated `jaas/` dir; daemon-translated on Windows-remote hosts. |
| `ZK_WORK_DIR` | `_daemon_mount_path(session basetemp)` | Unchanged (temp-dir artifacts). |
| `build: ./dockerfiles/{capture,certgen,kdc,tls-secrets-agent}` | relative to compose context | Resolve from the relocated `dockerfiles/` trees. |
| Compose file names | `docker-compose.base.yml`, `docker-compose.auth-<auth>.yml`, `docker-compose.features-capture.yml` | Unchanged (underscore→hyphen auth mapping preserved). |

## 3. Behavioral invariants

- Marker registration (`zk_version`, `zk_auth`, `zk_features`, `skip_if_zk_version`) and collection-time skip decisions are byte-identical (FR-011).
- `--zk-version` / `--zk-auth` / `--zk-features` CLI surface and their env fallbacks unchanged.
- `KazooZkEnv` and `ZkEnsemble` are frozen attrs classes with unchanged fields and construction.
- Fixture scopes unchanged: `docker_env`/`docker_compose` session-scoped; `zkensemble`, `zkchroot`, `zkclient`, `zksuperadmin_client` function-scoped; `check_skip_version_marker` autouse.
- `pytest_sessionfinish` interrupted-run capture probe behaves as today (best-effort, never fails the run).

## 4. Verification

Covered by `quickstart.md`: import checks for every name above, the plan-reference/history greps, the 100%-branch-coverage run, wheel/sdist inspection, and the integration-suite parity run.
