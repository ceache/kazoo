# Quickstart: Testing Resources Rationalization

**Feature**: [spec.md](spec.md) | **Date**: 2026-08-18

Runnable validation scenarios that prove the refactor works end-to-end. Run these after the implementation; details live in [contracts/modules.md](contracts/modules.md) and [data-model.md](data-model.md).

## Prerequisites

- A `docker compose` CLI (v2.12+) with a running Linux Docker daemon for the integration scenarios.
- `pip install -e '.[test]'`.

## V1 — Import surface intact (no Docker needed)

```bash
python - <<'PY'
from kazoo.testing.common import (
    ZKAuthMode, ZKFeature, ZK_DEFAULT_VERSION, FEATURE_JVM_PROPERTIES,
    AUTH_JVM_FLAGS, KazooZkEnv, ZkEnsemble,
    _assemble_tls_keylog, _evaluate_axis_markers,
)
from kazoo.testing.fixtures import (
    docker_env, docker_compose, zkensemble, zkchroot, zkclient,
    zksuperadmin_client, check_skip_version_marker, docker_compose_config,
    pytest_addoption, pytest_configure,
    pytest_collection_modifyitems, pytest_sessionfinish,
)
import kazoo.testing
import kazoo.testing.kazoo_ensemble  # must raise ModuleNotFoundError
PY
```

Expected: the imports succeed; `import kazoo.testing.kazoo_ensemble` raises `ModuleNotFoundError` (module deleted).

## V2 — Removed modules gone; no dangling references

```bash
test ! -e kazoo/tests/conftest.py
test ! -e kazoo/testing/kazoo_ensemble.py
rg -n "kazoo_ensemble|kazoo\.tests\.conftest" --glob '!specs/**' --glob '!CHANGES.md' .
```

Expected: both files absent; the grep returns nothing in code/config (only allowed hit: the CHANGES.md release note).

## V3 — Resources relocated and resolvable

```bash
python - <<'PY'
from importlib import resources
assert resources.files("kazoo.testing").joinpath("docker-compose.base.yml").is_file()
assert resources.files("kazoo.testing").joinpath("jaas/sasl-digest.conf").is_file()
assert resources.files("kazoo.testing").joinpath("dockerfiles/capture/Dockerfile").is_file()
assert resources.files("kazoo.testing").joinpath("dockerfiles/kdc").is_dir()
print("resources OK")
PY
```

Expected: `resources OK`. The old locations must be empty of framework resources:
`ls kazoo/tests/integ/` shows only `conftest.py` and `test_*.py`.

## V4 — Comment/docstring cleanup (zero plan references, no history)

```bash
rg -n "\(US[0-9]+|\(FR-[0-9]+|\(R-[0-9]+|\(SC-[0-9]+|quickstart|the plan|formerly|used to|was removed \(see" kazoo/testing kazoo/tests
```

Expected: zero matches in every file type (Python, YAML, Dockerfiles, entrypoint scripts, JAAS configs) under `kazoo/testing` and `kazoo/tests`. (A legitimate test asserting a legacy SASL string form may still contain the word "legacy" — that is kept; only narrative/plan references are removed.)

## V5 — Unit suite: 100% branch coverage of `common.py` pure functions (no Docker)

```bash
pytest kazoo/tests/unit/test_testing.py \
  --cov=kazoo.testing.common --cov-branch --cov-report=term-missing -q
```

Expected: all tests pass in seconds; `Missing` column empty for the pure functions enumerated in `data-model.md`; branch coverage 100%. Docker/subprocess-bound helpers (`_ensure_docker_available`, `_ensure_linux_docker_backend`, `_build_capture_images`, `_export_krb5_client_env`) are outside the pure-function target set.

## V6 — Integration parity (plain axis + a representative auth + capture)

```bash
pytest kazoo/tests/integ -q --zk-auth=plain --zk-features=standard
pytest kazoo/tests/integ -q --zk-auth=tls
pytest kazoo/tests/integ -q --zk-auth=plain --zk-features=capture
```

Expected: pass/skip/fail counts identical to the pre-refactor branch for the same commands (SC-001); capture artifacts land under the session `ZK_WORK_DIR` as before. The moved compose files, JAAS mounts, and sidecar images provision identically (FR-003).

## V7 — Installed-package resolution (wheel + sdist)

```bash
python -m build
python -m zipfile -l dist/*.whl | rg "kazoo/testing/(docker-compose|jaas|dockerfiles)" | head
tar -tzf dist/*.tar.gz | rg "kazoo/testing/(docker-compose|jaas|dockerfiles)" | head
```

Expected: the relocated resources appear in both artifacts (FR-004/SC-002). Optionally: create a venv, `pip install dist/*.whl`, and re-run V1/V3 against the installed package.

## V8 — Quality gates

```bash
flake8 kazoo/testing kazoo/tests
black -l 79 --check kazoo/testing kazoo/tests
mypy --config-file pyproject.toml kazoo
```

Expected: clean (SC-007). Docs build check (optional companion): `sphinx-build -b html docs docs/_build` resolves the updated `automodule`/`:mod:` targets.

## V9 — Windows sanity (CI)

Push the branch and run the `test_windows` job in `.github/workflows/testing.yml`. Expected: the WSL2 dockerd bring-up resolves the compose context from the installed `kazoo.testing` package and the plain-axis integration run passes — proving the relocated context keeps working with `_daemon_mount_path` on Windows-remote `DOCKER_HOST` (FR-011).
